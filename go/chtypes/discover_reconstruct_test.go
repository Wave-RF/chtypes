package chtypes

// Unit coverage for reconstructDDLWith that needs no loaded artifact —
// Library.ReconstructDDL wraps it to supply its own QuoteIdentifier.
//
// Python's _reconstruct_ddl, TypeScript's reconstructDdlWith and Rust's
// reconstruct_ddl_with each take the quoter as an argument, so a test can
// pass a marker quoter and pin this package's own logic — the kinds, the
// expressions, the refusals — while pinning nothing about spelling. Go's
// ReconstructDDL used to call l.QuoteIdentifier inline and had no seam to
// drive that way (issue #148); TestReconstructDDLSpellsKindsThroughTheLibrary
// (quoting_boundaries_test.go) is this package's only other coverage of the
// same properties, and it needs a real artifact.
//
// marker, below, returns something deliberately un-ClickHouse-like — never a
// back-quoted spelling — so these tests pin this file's own logic and never
// the rule for how a name is quoted. That rule lives behind an artifact
// (issue #119); pinning any spelling of it here would re-assert what #52
// deleted. Mirrors python/tests/test_discover_reconstruct.py and
// rust/src/discover.rs's marker test helper.

import (
	"errors"
	"strings"
	"testing"
)

// marker stands in for a library's own QuoteIdentifier: deliberately NOT a
// ClickHouse spelling (real quoting either leaves a name bare or wraps it in
// back-quotes, doubling any back-quote inside), so a case built on it cannot
// also pass with real ClickHouse quoting — it proves reconstructDDLWith
// called THIS function and used its answer verbatim.
func marker(name string) (string, error) {
	return "<" + name + ">", nil
}

// errQuoterBoom proves the quoter's own error is returned unchanged, rather
// than swallowed or rewrapped.
var errQuoterBoom = errors.New("marker quoter refused")

func TestReconstructDDLWithSpellsKindsAndLeavesTheNameToTheQuoter(t *testing.T) {
	cols := []DiscoveredColumn{
		{Name: "id", Type: "UInt64"},
		{Name: "ts", Type: "DateTime", DefaultKind: "DEFAULT", DefaultExpression: "now()"},
		{Name: "n.a", Type: "Array(Int64)"},
		{Name: "e", Type: "UInt8", DefaultKind: "EPHEMERAL"},
		{Name: "m", Type: "UInt64", DefaultKind: "MATERIALIZED", DefaultExpression: "id + 1"},
	}
	ddl, err := reconstructDDLWith(cols, marker)
	if err != nil {
		t.Fatalf("reconstructDDLWith: %v", err)
	}
	// Every name is whatever the quoter answered, verbatim: this function no
	// longer decides which names are spelled how.
	for _, want := range []string{
		"<n.a> Array(Int64)",
		"<ts> DateTime DEFAULT now()",
		"<e> UInt8 EPHEMERAL",
		"<m> UInt64 MATERIALIZED id + 1",
		"<id> UInt64",
	} {
		if !strings.Contains(ddl, want) {
			t.Errorf("reconstruction lost %q:\n%s", want, ddl)
		}
	}
}

func TestReconstructDDLWithErrorSurfaces(t *testing.T) {
	// DEFAULT/MATERIALIZED/ALIAS require an expression.
	for _, kind := range []string{"DEFAULT", "MATERIALIZED", "ALIAS"} {
		if _, err := reconstructDDLWith([]DiscoveredColumn{{Name: "x", Type: "UInt8", DefaultKind: kind}}, marker); err == nil {
			t.Errorf("%s without an expression must error", kind)
		}
	}

	// An unknown kind errors rather than passing through.
	if _, err := reconstructDDLWith([]DiscoveredColumn{{Name: "x", Type: "UInt8", DefaultKind: "WEIRD"}}, marker); err == nil {
		t.Error("an unknown default_kind must error")
	}

	// An expression with no kind errors: it would silently drop semantics.
	if _, err := reconstructDDLWith([]DiscoveredColumn{{Name: "x", Type: "UInt8", DefaultExpression: "1"}}, marker); err == nil {
		t.Error("a default_expression with no default_kind must error")
	}

	// A column missing its name or type errors.
	if _, err := reconstructDDLWith([]DiscoveredColumn{{Type: "UInt8"}}, marker); err == nil {
		t.Error("a column with no name must error")
	}
	if _, err := reconstructDDLWith([]DiscoveredColumn{{Name: "x"}}, marker); err == nil {
		t.Error("a column with no type must error")
	}

	// No columns is a wrong table, not an empty DDL.
	if _, err := reconstructDDLWith(nil, marker); err == nil {
		t.Error("no columns must error")
	}

	// EPHEMERAL may omit its expression, and may carry one.
	got, err := reconstructDDLWith([]DiscoveredColumn{
		{Name: "e", Type: "UInt8", DefaultKind: "EPHEMERAL", DefaultExpression: "7"},
	}, marker)
	if err != nil {
		t.Fatalf("EPHEMERAL with an expression: %v", err)
	}
	if want := "<e> UInt8 EPHEMERAL 7"; got != want {
		t.Errorf("EPHEMERAL with an expression = %q, want %q", got, want)
	}

	// A quoter's own error surfaces unchanged.
	boom := func(string) (string, error) { return "", errQuoterBoom }
	if _, err := reconstructDDLWith([]DiscoveredColumn{{Name: "x", Type: "UInt8"}}, boom); !errors.Is(err, errQuoterBoom) {
		t.Errorf("quoter error = %v, want %v", err, errQuoterBoom)
	}
}
