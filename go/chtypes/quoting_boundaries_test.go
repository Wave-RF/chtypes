package chtypes

// The quoting trio, measured against real artifacts (issue #119, from #52).
//
// Five things are pinned here, and none of them is a spelling:
//
//  1. The per-line boundaries. QuoteIdentifierIfNeeded answers the LOADED
//     BUILD's own rule, and that rule moves between ClickHouse lines. The
//     0.3.0 CHANGELOG states which names move where; these cases execute that
//     statement against whatever lines the registry holds, on BOTH sides of
//     every boundary.
//  2. An empty identifier comes back quoted, never bare (include/chtypes.h).
//  3. A literal carrying a NUL byte survives end to end — the one case where
//     the counted-input contract is load-bearing, because a strlen would
//     truncate at the NUL and nothing else would say so.
//  4. Two loaded libraries answer for themselves, asked in one process and
//     interleaved. Per-line divergence is the entire reason these calls hang
//     off a library rather than off the package, and one library cannot show
//     it.
//  5. ReconstructDDL spells the kinds, the expressions and the refusals, and
//     leaves every NAME to the library. reconstructDDLWith
//     (discover_reconstruct_test.go) pins that no-artifact with a marker
//     quoter, the same way the other three bindings do; the test here
//     confirms the real wrapper — ReconstructDDL calling l.QuoteIdentifier —
//     with the expected spelling asked of that same library rather than
//     written down.
//
// HOW A SPELLING IS NEVER WRITTEN DOWN HERE. Every expectation is stated as
// one of the library's OWN two answers: QuoteIdentifier always quotes, so it
// IS this build's quoted spelling, and the input itself is the bare spelling.
// A case asserts answer == lib.QuoteIdentifier(name) or answer == name and
// never names a quote character — which is what keeps
// scripts/check-quoting-passthrough.py true and keeps these cases from
// re-asserting the hand-written rule issue #52 deleted. Each line is first
// checked to spell the two forms DIFFERENTLY, so "quoted" and "bare" cannot
// both be satisfied by the same bytes.
//
// THE TWO ENUMERATIONS BELOW ARE EXPECTATIONS, NOT A RULE. Nothing here
// computes which names a build quotes; the sets say what the release
// MEASURED, per line, and a disagreement is a finding about the artifact
// rather than a test to adjust. They are closed downward on purpose: the
// lines named are the published lines that sit below a boundary, and any
// other line is at or above the last one, so a line published after this was
// written reads as "quotes all three" rather than silently dropping out of
// the count.
//
// COUNTS, AND WHY A QUIET RUN IS A FAILURE. Every case counts what it
// actually ran and asserts the total. Without a registry every case here
// skips, loudly, by name, through the same skipNoArtifacts every other
// registry test uses; WITH one, a case that observed only one side of a
// boundary — or only one library — FAILS by name rather than passing on the
// half it could see. A boundary case that skips forever is exactly the
// failure these tests exist to prevent.

import (
	"strings"
	"testing"
)

// The 0.3.0 CHANGELOG's claim, split into the part that holds everywhere and
// the part that moves between lines.
var (
	quotedOnEveryLine = []string{"all", "distinct", "table", "null"}
	bareOnEveryLine   = []string{"where"}
	movesBetweenLines = []string{"select", "from", "values"}
)

func everyName() []string {
	out := append([]string{}, quotedOnEveryLine...)
	out = append(out, bareOnEveryLine...)
	return append(out, movesBetweenLines...)
}

// linesBelowEveryBoundary are the published lines below every boundary: all
// three of the moving names bare. linesQuotingSelectOnly quote `select` and
// not yet `from`/`values`.
var (
	linesBelowEveryBoundary = map[string]bool{"24.8": true, "25.3": true, "25.8": true, "25.10": true, "26.2": true}
	linesQuotingSelectOnly  = map[string]bool{"26.3": true, "26.4": true}
)

// expectedBare is whether this line is expected to spell name bare, per the
// CHANGELOG.
func expectedBare(line, name string) bool {
	for _, n := range quotedOnEveryLine {
		if n == name {
			return false
		}
	}
	for _, n := range bareOnEveryLine {
		if n == name {
			return true
		}
	}
	// Anything left is one of the names the release says MOVES between lines,
	// and nothing else may reach the per-line answer below.
	moving := false
	for _, n := range movesBetweenLines {
		if n == name {
			moving = true
		}
	}
	if !moving {
		panic("chtypes test: " + name + " has no expectation in this file")
	}
	if linesBelowEveryBoundary[line] {
		return true
	}
	if linesQuotingSelectOnly[line] {
		return name != "select"
	}
	return false
}

type loadedLine struct {
	line string
	lib  *Library
}

// loadedLines opens every line the test registry can answer for, in release
// order. A line that refuses to open — an artifact from an older ABI revision
// left on the search path, say — is reported and left out rather than failing
// every case here: whether enough lines opened is decided by each test, by
// name, where the reason can be stated.
func loadedLines(t *testing.T) []loadedLine {
	t.Helper()
	dir := testRegistryDir(t)
	r, err := NewRegistry(dir)
	if err != nil {
		skipNoArtifacts(t, dir, "the registry did not load: "+err.Error())
	}
	versions := r.Versions()
	if len(versions) == 0 {
		skipNoArtifacts(t, dir, "the registry discovered no versions")
	}
	var out []loadedLine
	for _, v := range versions {
		lib, err := r.For(Version(v))
		if err != nil {
			t.Logf("line %s did not open and is not measured here: %v", v, err)
			continue
		}
		out = append(out, loadedLine{line: v, lib: lib})
	}
	if len(out) == 0 {
		skipNoArtifacts(t, dir, "no discovered line opened")
	}
	return out
}

// bothForms is (the always-quoted spelling, the if-needed answer) for one
// name. The first is this build's own quoted form — that is what
// QuoteIdentifier IS — so a case never has to know what quoting looks like.
func bothForms(t *testing.T, lib *Library, name string) (always, answer string) {
	t.Helper()
	always, err := lib.QuoteIdentifier(name)
	if err != nil {
		t.Fatalf("QuoteIdentifier(%q): %v", name, err)
	}
	answer, err = lib.QuoteIdentifierIfNeeded(name)
	if err != nil {
		t.Fatalf("QuoteIdentifierIfNeeded(%q): %v", name, err)
	}
	return always, answer
}

func TestQuotingBoundariesPerLine(t *testing.T) {
	loaded := loadedLines(t)
	names := everyName()
	ran, below, above := 0, 0, 0
	for _, l := range loaded {
		if linesBelowEveryBoundary[l.line] {
			below++
		} else if !linesQuotingSelectOnly[l.line] {
			above++
		}
		for _, name := range names {
			always, answer := bothForms(t, l.lib, name)
			if always == name {
				t.Fatalf("%s: the always-quoted form of %q is the bare name, so this case cannot tell quoted from bare", l.line, name)
			}
			if expectedBare(l.line, name) {
				if answer != name {
					t.Errorf("%s: %q -> %q, documented bare", l.line, name, answer)
				}
			} else if answer != always {
				t.Errorf("%s: %q -> %q, documented as this build's quoted form %q", l.line, name, answer, always)
			}
			ran++
		}
	}
	if want := len(names) * len(loaded); ran != want {
		t.Fatalf("ran %d cases, want %d — a case was skipped inside the loop", ran, want)
	}
	if ran == 0 {
		t.Fatal("no line was examined")
	}
	// Both sides, or nothing is being measured. CI fetches 24.8 alongside the
	// newest -lts and -stable precisely so this holds.
	if below == 0 {
		t.Fatalf("the registry holds no line below every documented boundary (24.8 is one), so the boundary cannot be observed at all — scripts/fetch.sh 24.8 installs one. Lines loaded: %v", lineNames(loaded))
	}
	if above == 0 {
		t.Fatalf("the registry holds no line at or above the last documented boundary, so the quoted side of it cannot be observed — scripts/fetch.sh 26.8 installs one. Lines loaded: %v", lineNames(loaded))
	}
	t.Logf("%d cases over %v (%d below every boundary, %d at or above the last)", ran, lineNames(loaded), below, above)
}

func lineNames(loaded []loadedLine) []string {
	out := make([]string, 0, len(loaded))
	for _, l := range loaded {
		out = append(out, l.line)
	}
	return out
}

func TestEmptyIdentifierComesBackQuoted(t *testing.T) {
	loaded := loadedLines(t)
	ran := 0
	for _, l := range loaded {
		always, answer := bothForms(t, l.lib, "")
		if answer == "" {
			t.Errorf("%s: an empty identifier came back empty, which is bare", l.line)
		}
		if answer != always {
			t.Errorf("%s: an empty identifier -> %q, want this build's quoted form %q", l.line, answer, always)
		}
		ran++
	}
	if ran != len(loaded) || ran == 0 {
		t.Fatalf("ran %d cases over %d lines — no line answered for an empty identifier", ran, len(loaded))
	}
}

func TestLiteralCarryingANULByteSurvives(t *testing.T) {
	loaded := loadedLines(t)
	ran := 0
	for _, l := range loaded {
		plain, err := l.lib.QuoteLiteral("a")
		if err != nil {
			t.Fatalf("%s: QuoteLiteral: %v", l.line, err)
		}
		withNUL, err := l.lib.QuoteLiteral("a\x00b")
		if err != nil {
			t.Fatalf("%s: QuoteLiteral with a NUL byte: %v", l.line, err)
		}
		// A strlen'd input would quote just the leading "a" and say nothing.
		if withNUL == plain {
			t.Errorf("%s: the input was truncated at the NUL byte: %q", l.line, withNUL)
		}
		if len(withNUL) <= len(plain) {
			t.Errorf("%s: %q is no longer than %q", l.line, withNUL, plain)
		}
		if !strings.Contains(withNUL, "b") {
			t.Errorf("%s: the byte after the NUL is missing from %q", l.line, withNUL)
		}
		// The header's other half: every byte the server escapes comes back
		// escaped, so the ANSWER is NUL-free even when the input was not.
		if strings.ContainsRune(withNUL, 0) {
			t.Errorf("%s: the answer %q carries a NUL byte", l.line, withNUL)
		}
		// Same delimiters as an ordinary value: this is one literal, not two.
		if withNUL[0] != plain[0] || withNUL[len(withNUL)-1] != plain[len(plain)-1] {
			t.Errorf("%s: %q is not delimited like %q", l.line, withNUL, plain)
		}
		ran++
	}
	if ran != len(loaded) || ran == 0 {
		t.Fatalf("ran %d cases over %d lines — no line quoted a literal carrying a NUL byte", ran, len(loaded))
	}
}

func TestQuoteIdentifierIfNeededAcrossTwoLibraries(t *testing.T) {
	loaded := loadedLines(t)
	if len(loaded) < 2 {
		t.Fatalf("this case needs TWO libraries in one process — a single library cannot show a cache-across-versions bug, which is the whole reason these calls hang off a library. Lines loaded: %v", lineNames(loaded))
	}
	older, newer := loaded[0], loaded[len(loaded)-1]
	names := everyName()

	ran := 0
	rounds := []map[string][2]string{{}, {}}
	// Interleaved, and then interleaved again: each library is asked the same
	// name after the other one has answered it, so an answer cached across
	// versions would show up as one library repeating the other's.
	for _, round := range rounds {
		for _, name := range names {
			a, err := older.lib.QuoteIdentifierIfNeeded(name)
			if err != nil {
				t.Fatalf("%s: %v", older.line, err)
			}
			b, err := newer.lib.QuoteIdentifierIfNeeded(name)
			if err != nil {
				t.Fatalf("%s: %v", newer.line, err)
			}
			round[name] = [2]string{a, b}
			ran += 2
		}
	}
	for _, name := range names {
		if rounds[0][name] != rounds[1][name] {
			t.Errorf("%s/%s: asking one library changed the other's answer for %q: %v then %v", older.line, newer.line, name, rounds[0][name], rounds[1][name])
		}
	}
	if want := 2 * 2 * len(names); ran != want {
		t.Fatalf("ran %d calls, want %d — a name was skipped inside the loop", ran, want)
	}

	// And where the two lines sit on opposite sides of a boundary, they must
	// DISAGREE — the divergence the library-scoped API exists for.
	divergent := 0
	for _, name := range names {
		if expectedBare(older.line, name) == expectedBare(newer.line, name) {
			continue
		}
		divergent++
		if rounds[0][name][0] == rounds[0][name][1] {
			t.Errorf("%q is documented as differing between %s and %s, and both answered %q", name, older.line, newer.line, rounds[0][name][0])
		}
	}
	if divergent == 0 {
		t.Fatalf("%s and %s sit on the same side of every documented boundary, so no divergence can be observed — fetch a line below 26.3 (24.8) alongside the newest one", older.line, newer.line)
	}
	t.Logf("%d interleaved calls over %s and %s, %d names documented as divergent", ran, older.line, newer.line, divergent)
}

// TestReconstructDDLSpellsKindsThroughTheLibrary confirms the real
// ReconstructDDL wrapper — reconstructDDLWith fed l.QuoteIdentifier — against
// a loaded library, with every expected NAME asked of that same library
// rather than written down. discover_reconstruct_test.go pins the same
// properties — the kinds, the expressions and the refusals — no-artifact,
// against reconstructDDLWith directly with a marker quoter, the way the other
// three bindings' unit tests do.
func TestReconstructDDLSpellsKindsThroughTheLibrary(t *testing.T) {
	loaded := loadedLines(t)
	lib := loaded[len(loaded)-1].lib
	q := func(name string) string {
		spelled, err := lib.QuoteIdentifier(name)
		if err != nil {
			t.Fatalf("QuoteIdentifier(%q): %v", name, err)
		}
		return spelled
	}

	ddl, err := lib.ReconstructDDL([]DiscoveredColumn{
		{Name: "id", Type: "UInt64"},
		{Name: "ts", Type: "DateTime", DefaultKind: "DEFAULT", DefaultExpression: "now()"},
		{Name: "n.a", Type: "Array(Int64)"},
		{Name: "e", Type: "UInt8", DefaultKind: "EPHEMERAL"},
		{Name: "m", Type: "UInt64", DefaultKind: "MATERIALIZED", DefaultExpression: "id + 1"},
	})
	if err != nil {
		t.Fatalf("ReconstructDDL: %v", err)
	}
	// Every name is whatever the library answered, verbatim: this package no
	// longer decides which names are spelled how.
	ran := 0
	for _, want := range []string{
		q("n.a") + " Array(Int64)",
		q("ts") + " DateTime DEFAULT now()",
		q("e") + " UInt8 EPHEMERAL",
		q("m") + " UInt64 MATERIALIZED id + 1",
		q("id") + " UInt64",
	} {
		if !strings.Contains(ddl, want) {
			t.Errorf("reconstruction lost %q:\n%s", want, ddl)
		}
		ran++
	}

	// The refusals, which are this package's own and not the library's.
	refusals := []struct {
		what string
		cols []DiscoveredColumn
	}{
		{"DEFAULT without an expression", []DiscoveredColumn{{Name: "x", Type: "UInt8", DefaultKind: "DEFAULT"}}},
		{"MATERIALIZED without an expression", []DiscoveredColumn{{Name: "x", Type: "UInt8", DefaultKind: "MATERIALIZED"}}},
		{"ALIAS without an expression", []DiscoveredColumn{{Name: "x", Type: "UInt8", DefaultKind: "ALIAS"}}},
		{"an unknown kind", []DiscoveredColumn{{Name: "x", Type: "UInt8", DefaultKind: "WEIRD"}}},
		{"an expression with no kind", []DiscoveredColumn{{Name: "x", Type: "UInt8", DefaultExpression: "1"}}},
		{"a column with no type", []DiscoveredColumn{{Name: "x"}}},
		{"no columns at all", nil},
	}
	for _, r := range refusals {
		if _, err := lib.ReconstructDDL(r.cols); err == nil {
			t.Errorf("%s must be refused, not reconstructed", r.what)
		}
		ran++
	}

	// EPHEMERAL may omit its expression, and may carry one.
	got, err := lib.ReconstructDDL([]DiscoveredColumn{
		{Name: "e", Type: "UInt8", DefaultKind: "EPHEMERAL", DefaultExpression: "7"},
	})
	if err != nil {
		t.Fatalf("EPHEMERAL with an expression: %v", err)
	}
	if want := q("e") + " UInt8 EPHEMERAL 7"; got != want {
		t.Errorf("EPHEMERAL with an expression = %q, want %q", got, want)
	}
	ran++

	if want := 5 + len(refusals) + 1; ran != want {
		t.Fatalf("ran %d cases, want %d — a case was skipped", ran, want)
	}
}
