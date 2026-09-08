package chtypes

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"testing"
)

// The public golden set — goldens/cases.json at the repository root — is the
// SDK's smoke proof: a few dozen cases whose expectations were produced by the
// library itself and agreed on by every ClickHouse version in the generating
// registry (chtypes-core: tests/conformance/go/cmd/goldens-gen). Every SDK
// runs the same file, so the four bindings are held to one answer. It is not
// the corpus; that lives with the rigs.

type goldenFile struct {
	Schema int          `json:"schema"`
	Cases  []goldenCase `json:"cases"`
}

type goldenCase struct {
	ID       string            `json:"id"`
	DDL      string            `json:"ddl"`
	Format   string            `json:"format"`
	Body     string            `json:"body"`
	Settings map[string]string `json:"settings"`
	Filter   string            `json:"filter"`
	Expect   struct {
		CompileErrorCode int    `json:"compile_error_code"`
		Outcome          string `json:"outcome"`
		ErrCode          int    `json:"err_code"`
		Rows             []struct {
			Outcome     string              `json:"outcome"`
			ErrCode     int                 `json:"err_code"`
			Values      map[string]string   `json:"values"`
			Nulls       []string            `json:"nulls"`
			Transformed []map[string]string `json:"transformed"`
			Substituted []string            `json:"substituted"`
			Computed    map[string]string   `json:"computed"`
		} `json:"rows"`
		Verdicts []string `json:"verdicts"`
	} `json:"expect"`
}

var goldenFormats = map[string]Format{
	"JSONEachRow": JSONEachRow, "CSV": CSV, "TSV": TSV, "Values": Values, "JSONCompactEachRow": JSONCompactEachRow,
}

func loadGoldens(t *testing.T) goldenFile {
	t.Helper()
	path := os.Getenv("CHTYPES_GOLDENS")
	if path == "" {
		_, self, _, ok := runtime.Caller(0)
		if !ok {
			t.Fatal("cannot resolve the source path")
		}
		path = filepath.Join(filepath.Dir(self), "..", "..", "goldens", "cases.json")
	}
	blob, err := os.ReadFile(path)
	if err != nil {
		// The golden set ships with the repository, two levels above this
		// package, not inside the Go module: a copy of go/ alone (a module
		// cache, the standalone check without CHTYPES_GOLDENS) has no file to
		// read. That is a loud skip, not a failure — and never a silent pass.
		t.Skipf("golden set not found: %v (set CHTYPES_GOLDENS to goldens/cases.json)", err)
	}
	var g goldenFile
	if err := json.Unmarshal(blob, &g); err != nil {
		t.Fatalf("golden set does not parse: %v", err)
	}
	if g.Schema != 1 {
		t.Fatalf("golden set schema %d; this test reads schema 1", g.Schema)
	}
	if len(g.Cases) == 0 {
		t.Fatal("golden set holds no cases")
	}
	return g
}

func verdictName(v Verdict) string {
	switch v {
	case VerdictTrue:
		return "true"
	case VerdictFalse:
		return "false"
	case VerdictError:
		return "error"
	default:
		return "decline"
	}
}

func TestGoldens(t *testing.T) {
	g := loadGoldens(t)
	r := testRegistry(t)
	checked := 0
	for _, v := range r.Versions() {
		lib, err := r.For(Version(v))
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range g.Cases {
			f, ok := goldenFormats[c.Format]
			if !ok {
				t.Fatalf("%s: unknown format %q", c.ID, c.Format)
			}
			t.Run(v+"/"+c.ID, func(t *testing.T) {
				cs, err := lib.CompileDDL(c.DDL)
				if c.Expect.CompileErrorCode != 0 {
					se, ok := err.(*SchemaError)
					if !ok {
						t.Fatalf("compile: want *SchemaError code %d, got %v", c.Expect.CompileErrorCode, err)
					}
					if se.Code != c.Expect.CompileErrorCode {
						t.Fatalf("compile code = %d, want %d", se.Code, c.Expect.CompileErrorCode)
					}
					return
				}
				if err != nil {
					t.Fatalf("compile: %v", err)
				}
				defer cs.Close()
				if c.Filter != "" {
					fl, err := cs.CompileFilter(c.Filter)
					if err != nil {
						t.Fatalf("compile filter: %v", err)
					}
					defer fl.Close()
					fr, err := fl.Rows(f, []byte(c.Body), c.Settings)
					if err != nil {
						t.Fatalf("filter rows: %v", err)
					}
					if fr.Outcome != FilterOK || c.Expect.Outcome != "ok" {
						t.Fatalf("filter outcome = %v, want %s", fr.Outcome, c.Expect.Outcome)
					}
					got := make([]string, 0, len(fr.Verdicts))
					for _, vd := range fr.Verdicts {
						got = append(got, verdictName(vd))
					}
					if !equalStrings(got, c.Expect.Verdicts) {
						t.Fatalf("verdicts = %v, want %v", got, c.Expect.Verdicts)
					}
					return
				}
				br, err := cs.Rows(f, []byte(c.Body), c.Settings)
				if err != nil {
					t.Fatalf("rows: %v", err)
				}
				if br.Outcome.String() != c.Expect.Outcome || br.ErrCode != c.Expect.ErrCode {
					t.Fatalf("batch = %s/%d, want %s/%d (%s)", br.Outcome, br.ErrCode, c.Expect.Outcome, c.Expect.ErrCode, br.ErrMsg)
				}
				if len(br.Rows) != len(c.Expect.Rows) {
					t.Fatalf("%d row(s), want %d", len(br.Rows), len(c.Expect.Rows))
				}
				for i, row := range br.Rows {
					want := c.Expect.Rows[i]
					if row.Outcome.String() != want.Outcome {
						t.Fatalf("row %d outcome = %s, want %s (%s)", i, row.Outcome, want.Outcome, row.ErrMsg)
					}
					if want.Outcome == "rejected" || want.Outcome == "skipped" {
						if row.ErrCode != want.ErrCode {
							t.Fatalf("row %d code = %d, want %d (%s)", i, row.ErrCode, want.ErrCode, row.ErrMsg)
						}
						continue
					}
					var nulls []string
					for _, val := range row.Values {
						if w, ok := want.Values[val.Column]; !ok || w != val.Text {
							t.Fatalf("row %d %s = %q, want %q", i, val.Column, val.Text, w)
						}
						if val.Null {
							nulls = append(nulls, val.Column)
						}
					}
					if len(row.Values) != len(want.Values) {
						t.Fatalf("row %d has %d value(s), want %d", i, len(row.Values), len(want.Values))
					}
					sort.Strings(nulls)
					if !equalStrings(nulls, want.Nulls) {
						t.Fatalf("row %d nulls = %v, want %v", i, nulls, want.Nulls)
					}
					var tr []string
					for _, x := range row.Transformed {
						tr = append(tr, x.Column+":"+x.Reason)
					}
					var wtr []string
					for _, x := range want.Transformed {
						wtr = append(wtr, x["column"]+":"+x["reason"])
					}
					sort.Strings(tr)
					sort.Strings(wtr)
					if !equalStrings(tr, wtr) {
						t.Fatalf("row %d transformed = %v, want %v", i, tr, wtr)
					}
					var sub []string
					for _, s := range row.Substituted {
						sub = append(sub, s.Column)
					}
					sort.Strings(sub)
					if !equalStrings(sub, want.Substituted) {
						t.Fatalf("row %d substituted = %v, want %v", i, sub, want.Substituted)
					}
					comp := map[string]string{}
					for _, cp := range row.Computed {
						comp[cp.Column] = cp.Text
					}
					if len(comp) != len(want.Computed) {
						t.Fatalf("row %d computed = %v, want %v", i, comp, want.Computed)
					}
					for k, wv := range want.Computed {
						if comp[k] != wv {
							t.Fatalf("row %d computed %s = %q, want %q", i, k, comp[k], wv)
						}
					}
				}
			})
			checked++
		}
	}
	t.Logf("%d golden checks across %d version(s)", checked, len(r.Versions()))
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
