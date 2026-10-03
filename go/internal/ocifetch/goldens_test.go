package ocifetch

import (
	"strings"
	"testing"
)

func TestStatementRevision(t *testing.T) {
	cases := []struct {
		name    string
		payload string
		want    int64
		bad     bool
	}{
		{"zero", `{"predicate":{"revision":0}}`, 0, false},
		{"one", `{"predicate":{"revision":1}}`, 1, false},
		{"large", `{"predicate":{"revision":9007199254740991}}`, 9007199254740991, false},
		{"too large", `{"predicate":{"revision":9007199254740992}}`, 0, true},
		{"missing", `{"predicate":{"schema":1}}`, 0, true},
		{"bool", `{"predicate":{"revision":true}}`, 0, true},
		{"string", `{"predicate":{"revision":"2"}}`, 0, true},
		{"null", `{"predicate":{"revision":null}}`, 0, true},
		{"float", `{"predicate":{"revision":1.0}}`, 0, true},
		{"exponent", `{"predicate":{"revision":1e0}}`, 0, true},
		{"negative", `{"predicate":{"revision":-1}}`, 0, true},
		{"negative zero", `{"predicate":{"revision":-0}}`, 0, true},
		{"object", `{"predicate":{"revision":{}}}`, 0, true},
	}
	for _, c := range cases {
		got, err := statementRevision([]byte(c.payload))
		if c.bad {
			if err == nil {
				t.Errorf("%s: accepted %s as %d", c.name, c.payload, got)
			}
			continue
		}
		if err != nil || got != c.want {
			t.Errorf("%s: got (%d, %v), want %d", c.name, got, err, c.want)
		}
	}
}

func TestSelectGoldens(t *testing.T) {
	a := Digest("sha256:" + strings.Repeat("a", 64))
	b := Digest("sha256:" + strings.Repeat("b", 64))
	m := func(c byte) Digest { return Digest("sha256:" + strings.Repeat(string(c), 64)) }

	// The highest revision wins wherever it is listed.
	got, err := selectGoldens([]goldensCandidate{
		{manifest: m('1'), blob: a, revision: 1},
		{manifest: m('2'), blob: b, revision: 2},
		{manifest: m('3'), blob: a, revision: 0},
	}, "ref")
	if err != nil || got.revision != 2 || got.blob != b {
		t.Fatalf("highest: got (%+v, %v)", got, err)
	}

	// A tie of different documents is refused, naming both digests and the revision.
	_, err = selectGoldens([]goldensCandidate{
		{manifest: m('1'), blob: a, revision: 3},
		{manifest: m('2'), blob: b, revision: 3},
	}, "ref")
	if err == nil {
		t.Fatal("tie: expected an error")
	}
	for _, want := range []string{string(CodeArtifactCorrupt), string(a), string(b), "revision 3"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("tie error %q does not name %q", err, want)
		}
	}

	// The same document under the same revision (two bundles) is not a tie,
	// and the earliest-listed candidate is returned.
	got, err = selectGoldens([]goldensCandidate{
		{manifest: m('1'), blob: a, revision: 2, bundle: m('7')},
		{manifest: m('1'), blob: a, revision: 2, bundle: m('8')},
		{manifest: m('2'), blob: b, revision: 1},
	}, "ref")
	if err != nil || got.bundle != m('7') {
		t.Fatalf("same doc: got (%+v, %v)", got, err)
	}

	// A tie below the highest revision does not matter.
	got, err = selectGoldens([]goldensCandidate{
		{manifest: m('1'), blob: a, revision: 1},
		{manifest: m('2'), blob: b, revision: 1},
		{manifest: m('3'), blob: a, revision: 4},
	}, "ref")
	if err != nil || got.revision != 4 {
		t.Fatalf("lower tie: got (%+v, %v)", got, err)
	}
}
