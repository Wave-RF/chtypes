// abi_revision_test.go — the ABI-revision handshake, run against a
// wrong-revision fixture rather than assumed (#36).
//
// docs/reference/artifact.md step 5 is normative: an artifact whose
// chs_abi_revision() answers a value that is neither 0 nor the revision this
// binding was written against MUST be rejected, naming both numbers. This
// test drives that against two fixture registries the core repository's
// generator built from this repository's own include/chtypes.h: one artifact
// answering the header's revision plus one, one answering the header's own
// revision. Both expected numbers come from the fixture's fixture.json —
// which the generator wrote from the header — never from this package's own
// ABIRevision constant, because a case that read the constant under test
// could not catch the constant drifting.
package chtypes

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// abiRevisionFixture is fixture.json, trimmed to the fields this test needs.
type abiRevisionFixture struct {
	Root              string
	ABIRevision       int    `json:"abi_revision"`
	MismatchRevision  int    `json:"mismatch_revision"`
	ClickHouseVersion string `json:"clickhouse_version"`
	ClickHouseMinor   string `json:"clickhouse_minor"`
	Platform          string `json:"platform"`
}

// loadABIRevisionFixture resolves the fixture set from $CHTYPES_ABI_FIXTURES
// alone. There is no fallback build and no other search: a CI census greps
// test output for the exact skip text below, so it must appear verbatim.
func loadABIRevisionFixture(t *testing.T) abiRevisionFixture {
	t.Helper()
	root := os.Getenv("CHTYPES_ABI_FIXTURES")
	if root == "" {
		t.Skip("no ABI revision fixture: $CHTYPES_ABI_FIXTURES is unset (build one with abi-revision/gen.py build --out DIR --header include/chtypes.h)")
	}
	raw, err := os.ReadFile(filepath.Join(root, "fixture.json"))
	if err != nil {
		t.Fatalf("ABI revision fixture %s: cannot read fixture.json: %v", root, err)
	}
	var doc abiRevisionFixture
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("ABI revision fixture %s: fixture.json does not parse: %v", root, err)
	}
	doc.Root = root
	if doc.ABIRevision <= 0 {
		t.Fatalf("ABI revision fixture %s: fixture.json reports abi_revision %d, want > 0", root, doc.ABIRevision)
	}
	if doc.MismatchRevision != doc.ABIRevision+1 {
		t.Fatalf("ABI revision fixture %s: fixture.json reports mismatch_revision %d, want abi_revision+1 (%d)",
			root, doc.MismatchRevision, doc.ABIRevision+1)
	}
	return doc
}

// namesNumber is "the message says N", not "the message contains the digits
// of N": a refusal naming revision 4 must not be satisfied by the 4 inside a
// path like .../24.8/, and one naming 5 must not be satisfied by 15.
func namesNumber(message string, n int) bool {
	return regexp.MustCompile(fmt.Sprintf(`(^|[^0-9])%d([^0-9]|$)`, n)).MatchString(message)
}

// scrubFixtureRoot removes every occurrence of the fixture root — and its
// symlink-resolved spelling, since macOS's /tmp vs /private/tmp differ — from
// message. Without this, namesNumber can be satisfied by a bare digit inside
// the fixture PATH itself (a scratchpad directory name, a request ID) rather
// than by anything the refusal actually said, which makes "names both
// numbers" vacuous on some machines. Longest spelling first, so one is never
// a no-op prefix of the other.
func scrubFixtureRoot(message, root string) string {
	forms := []string{root}
	if resolved, err := filepath.EvalSymlinks(root); err == nil && resolved != root {
		forms = append(forms, resolved)
	}
	sort.Slice(forms, func(i, j int) bool { return len(forms[i]) > len(forms[j]) })
	scrubbed := message
	for _, f := range forms {
		scrubbed = strings.ReplaceAll(scrubbed, f, "<fixture>")
	}
	return scrubbed
}

// TestABIRevisionMismatchIsRefused opens an artifact whose
// chs_abi_revision() answers the header's revision plus one. Refusal must
// happen at the call that OPENS it — not a Library that later degrades — and
// the error must name both the mismatched revision and this binding's own.
//
// Construction reads manifests and dlopens nothing, so the open is asked for
// with WithPreload: the constructor-time spelling of that request, and the one
// whose failure is this test's verdict.
func TestABIRevisionMismatchIsRefused(t *testing.T) {
	doc := loadABIRevisionFixture(t)
	wrong := filepath.Join(doc.Root, "wrong-revision")

	_, err := NewRegistry(wrong, WithPreload(doc.ClickHouseMinor))
	if err == nil {
		t.Fatalf("NewRegistry(%s, WithPreload(%q)) succeeded for an artifact reporting ABI revision %d; "+
			"docs/reference/artifact.md step 5 requires it be refused, naming both %d and %d",
			wrong, doc.ClickHouseMinor, doc.MismatchRevision, doc.MismatchRevision, doc.ABIRevision)
	}
	msg := err.Error()
	// Checked with the fixture root scrubbed out: a path segment can itself
	// hold a standalone digit (this scratchpad's own request ID does), so
	// checking the raw message could pass on the path alone rather than on
	// anything the refusal said. The failure text below still quotes the
	// original, unscrubbed message.
	scrubbed := scrubFixtureRoot(msg, doc.Root)
	if !namesNumber(scrubbed, doc.MismatchRevision) {
		t.Fatalf("refusal for %s does not name the mismatched revision %d: %s", wrong, doc.MismatchRevision, msg)
	}
	if !namesNumber(scrubbed, doc.ABIRevision) {
		t.Fatalf("refusal for %s does not name this package's own revision %d: %s", wrong, doc.ABIRevision, msg)
	}
}

// TestABIRevisionControlLoads is the negative control: the same stub, built
// at the header's own revision, must load cleanly. Without this, a binding
// that refused every artifact would pass the mismatch case above for
// entirely the wrong reason.
func TestABIRevisionControlLoads(t *testing.T) {
	doc := loadABIRevisionFixture(t)
	at := filepath.Join(doc.Root, "at-revision")

	// Preloaded for symmetry with the mismatch case above: both verdicts then
	// come from the same call on the same code path, which is the whole point
	// of a control.
	r, err := NewRegistry(at, WithPreload(doc.ClickHouseMinor))
	if err != nil {
		t.Fatalf("NewRegistry(%s, WithPreload(%q)) refused an artifact reporting the header's own ABI revision %d: %v",
			at, doc.ClickHouseMinor, doc.ABIRevision, err)
	}
	// Registry and Library are both documented as never needing a
	// close/shutdown (docs/reference/bindings.md §Teardown; multiversion.go's
	// Library doc comment) — there is no API to call here.

	lib, err := r.For(Version(doc.ClickHouseMinor))
	if err != nil {
		t.Fatalf("Registry.For(%q) on the control registry %s: %v", doc.ClickHouseMinor, at, err)
	}
	if lib.ABIRevision != doc.ABIRevision {
		t.Fatalf("control library at %s reports ABI revision %d, fixture.json says %d", at, lib.ABIRevision, doc.ABIRevision)
	}
	if string(lib.Version) != doc.ClickHouseVersion {
		t.Fatalf("control library at %s reports ClickHouse version %q, fixture.json says %q", at, lib.Version, doc.ClickHouseVersion)
	}
}
