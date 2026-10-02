// conformance_test.go: the hand-written conformance runner (plan: "the
// conformance runner (small)" is one of the three hand-written pieces,
// alongside the loader and the eventual public API). It reads
// tests/fixtures/abi-v1/cases.json and scripts/abi-v1/build-stubs.sh's
// stubs.json manifest, drives every case through the generated invoke-by-name
// dispatcher (invoke_gen_test.go) and the hand-written loader (loader.go),
// and writes a spec/abi-v1/schema/report.schema.json-shaped report.
package abi1

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

type caseJSON struct {
	ID      string                 `json:"id"`
	Kind    string                 `json:"kind"`
	Fn      string                 `json:"fn"`
	Variant string                 `json:"variant"`
	OS      string                 `json:"os,omitempty"`
	Args    []Arg                  `json:"args"`
	Expect  map[string]interface{} `json:"expect"`
}

type casesDoc struct {
	Cases []caseJSON `json:"cases"`
}

type stubVariant struct {
	Path      string                 `json:"path"`
	Predicate map[string]interface{} `json:"predicate"`
	Reason    string                 `json:"reason"`
}

type stubsManifest struct {
	Variants map[string]stubVariant `json:"variants"`
}

type reportResult struct {
	ID     string `json:"id"`
	Pass   bool   `json:"pass"`
	Detail string `json:"detail,omitempty"`
}

type conformanceReport struct {
	Schema      int            `json:"schema"`
	Binding     string         `json:"binding"`
	Toolchain   string         `json:"toolchain"`
	OS          string         `json:"os"`
	CasesSHA256 string         `json:"cases_sha256"`
	Results     []reportResult `json:"results"`
}

// repoRoot finds the repository root from this file's own location
// (go/internal/abi1/conformance_test.go is three directories below it),
// never from the process's current working directory.
func repoRoot(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("conformance_test.go: runtime.Caller(0) failed")
	}
	return filepath.Join(filepath.Dir(file), "..", "..", "..")
}

// stubLibraryPath resolves a stub variant's library under THIS job's own
// CHTYPES_ABI1_STUBS directory, by file name only -- never variant.Path
// verbatim. stubs.json's path is written by the v1-abi-stubs job, on a
// DIFFERENT runner (and a different $RUNNER_TEMP) than the
// v1-abi-conformance job that downloads the artifact and sets
// CHTYPES_ABI1_STUBS, so a path recorded there (absolute or not) does not
// generally resolve here; only its file name does, and that is stable
// however build-stubs.sh spells the "path" field.
func stubLibraryPath(stubsDir string, variant stubVariant) string {
	return filepath.Join(stubsDir, filepath.Base(variant.Path))
}

// canonicalJSON is RFC-8785-adjacent, not the real thing: sorted object keys
// (encoding/json.Marshal's own rule for map[string]interface{}) and compact,
// unescaped-HTML output. It needs to agree with scripts/abi-v1/parity.py's
// compute_cases_hash (Python's json.dumps(..., sort_keys=True,
// separators=(",", ":"))) ONLY over this repository's own generated
// cases.json, which contains no floats, no non-ASCII and no HTML-special
// characters -- verified byte-for-byte against that function's own output
// while writing this file, not assumed.
func canonicalJSON(v interface{}) (string, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return "", err
	}
	return strings.TrimSuffix(buf.String(), "\n"), nil
}

func computeCasesHash(casesRaw interface{}) (string, error) {
	canon, err := canonicalJSON(casesRaw)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256([]byte(canon))
	return hex.EncodeToString(sum[:]), nil
}

// TestConformance is the v1-abi-conformance leg's entry point
// (scripts/abi-v1/conformance/go.sh runs `go test -run TestConformance`).
// Without CHTYPES_ABI1_STUBS it skips LOUDLY by name, exactly like every
// other no-registry test in this repository: it never passes silently and
// never fails for want of an environment nobody gave it.
func TestConformance(t *testing.T) {
	stubsDir := os.Getenv("CHTYPES_ABI1_STUBS")
	if stubsDir == "" {
		t.Skip("CHTYPES_ABI1_STUBS not set; skipping the ABI v1 conformance suite")
	}

	root := repoRoot(t)
	casesPath := filepath.Join(root, "tests", "fixtures", "abi-v1", "cases.json")
	rawCases, err := os.ReadFile(casesPath)
	if err != nil {
		t.Fatalf("reading %s: %v", casesPath, err)
	}

	// Two decodes of the SAME bytes: one generic (for the hash, which must
	// see the "cases" array exactly as JSON describes it) and one typed (for
	// driving the test). Both use UseNumber so a case's literal int fields
	// compare exactly, never through a lossy float64 round-trip.
	var generic map[string]interface{}
	gdec := json.NewDecoder(bytes.NewReader(rawCases))
	gdec.UseNumber()
	if err := gdec.Decode(&generic); err != nil {
		t.Fatalf("%s: %v", casesPath, err)
	}
	casesHash, err := computeCasesHash(generic["cases"])
	if err != nil {
		t.Fatalf("computing cases_sha256: %v", err)
	}

	var doc casesDoc
	tdec := json.NewDecoder(bytes.NewReader(rawCases))
	tdec.UseNumber()
	if err := tdec.Decode(&doc); err != nil {
		t.Fatalf("%s: %v", casesPath, err)
	}
	if len(doc.Cases) == 0 {
		t.Fatalf("%s: zero cases -- a suite that ran nothing must not pass", casesPath)
	}

	manifestPath := filepath.Join(stubsDir, "stubs.json")
	rawManifest, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatalf("reading %s: %v", manifestPath, err)
	}
	var manifest stubsManifest
	mdec := json.NewDecoder(bytes.NewReader(rawManifest))
	mdec.UseNumber()
	if err := mdec.Decode(&manifest); err != nil {
		t.Fatalf("%s: %v", manifestPath, err)
	}

	okVariant, haveOK := manifest.Variants["ok"]
	if !haveOK {
		t.Fatalf("%s: no %q variant", manifestPath, "ok")
	}
	okPredicate, err := json.Marshal(okVariant.Predicate)
	if err != nil {
		t.Fatalf("marshaling the %q variant's predicate: %v", "ok", err)
	}
	tbl, lerr := Load(LoadInput{
		LibraryPath: stubLibraryPath(stubsDir, okVariant),
		Predicate:   okPredicate,
		Platform:    fmt.Sprintf("%s-%s", runtime.GOOS, runtime.GOARCH),
	})
	if lerr != nil {
		t.Fatalf("loading the %q stub (required for every echo/status/handshake case): %v", "ok", lerr)
	}

	results := make([]reportResult, 0, len(doc.Cases))
	for _, c := range doc.Cases {
		// cases.schema.json's "os" field (added for loader.ctor-marker, which
		// refuses on linux but loads fine on darwin, D3's glibc step being
		// linux-only): a case naming an "os" that is not this leg's own is
		// OMITTED from the report entirely -- never run, never a result at
		// all, PASS included. A reported pass for a case that never ran is
		// exactly the pattern this repository refuses everywhere else (a
		// registry-gated test that "passes" by skipping is the house
		// example); it still runs as a Go subtest that skips, so `go test
		// -v` shows it, but no reportResult is appended for it.
		if c.OS != "" && c.OS != runtime.GOOS {
			t.Run(c.ID, func(t *testing.T) {
				t.Skipf("omitted: this case is os:%s, this leg is %s", c.OS, runtime.GOOS)
			})
			continue
		}
		pass, detail := true, ""
		t.Run(c.ID, func(t *testing.T) {
			switch c.Kind {
			case "handshake":
				pass, detail = runHandshakeCase(tbl, c)
			case "echo", "status":
				pass, detail = runEchoOrStatusCase(tbl, c)
			case "loader":
				pass, detail = runLoaderCase(stubsDir, manifest, c)
			default:
				pass, detail = false, fmt.Sprintf("unknown case kind %q", c.Kind)
			}
			if !pass {
				t.Error(detail)
			}
		})
		results = append(results, reportResult{ID: c.ID, Pass: pass, Detail: detail})
	}

	if reportPath := os.Getenv("CHTYPES_ABI1_REPORT"); reportPath != "" {
		toolchain := os.Getenv("CHTYPES_ABI1_TOOLCHAIN")
		if toolchain == "" {
			toolchain = "go.mod"
		}
		rep := conformanceReport{
			Schema:      1,
			Binding:     "go",
			Toolchain:   toolchain,
			OS:          fmt.Sprintf("%s-%s", runtime.GOOS, runtime.GOARCH),
			CasesSHA256: casesHash,
			Results:     results,
		}
		out, err := json.MarshalIndent(rep, "", "  ")
		if err != nil {
			t.Fatalf("marshaling the report: %v", err)
		}
		if err := os.WriteFile(reportPath, out, 0o644); err != nil {
			t.Fatalf("writing %s: %v", reportPath, err)
		}
		t.Logf("wrote %s (%d case result(s))", reportPath, len(results))
	}
}

func runHandshakeCase(tbl *Table, c caseJSON) (bool, string) {
	res, err := invoke(tbl, c.Fn, c.Args)
	if err != nil {
		return false, err.Error()
	}
	var problems []string
	if raw, ok := c.Expect["int"]; ok {
		wantNum, ok := raw.(json.Number)
		if !ok {
			return false, fmt.Sprintf("expect.int is not a number: %v", raw)
		}
		want, err := wantNum.Int64()
		if err != nil {
			return false, fmt.Sprintf("expect.int: %v", err)
		}
		switch {
		case res.Int == nil:
			problems = append(problems, "no int result")
		case *res.Int != want:
			problems = append(problems, fmt.Sprintf("int = %d, want %d", *res.Int, want))
		}
	}
	if raw, ok := c.Expect["contains"]; ok {
		want, _ := raw.(string)
		switch {
		case res.Str == nil:
			problems = append(problems, "no string result")
		case !strings.Contains(*res.Str, want):
			problems = append(problems, fmt.Sprintf("%q does not contain %q", *res.Str, want))
		}
	}
	if raw, ok := c.Expect["non_empty"]; ok {
		if want, _ := raw.(bool); want && (res.Str == nil || *res.Str == "") {
			problems = append(problems, "expected a non-empty string result")
		}
	}
	if len(problems) > 0 {
		return false, strings.Join(problems, "; ")
	}
	return true, ""
}

func runEchoOrStatusCase(tbl *Table, c caseJSON) (bool, string) {
	res, err := invoke(tbl, c.Fn, c.Args)
	if err != nil {
		return false, err.Error()
	}
	var problems []string

	wantStatus, _ := c.Expect["status"].(string)
	if res.Status != wantStatus {
		problems = append(problems, fmt.Sprintf("status = %s, want %s", res.Status, wantStatus))
	}

	if rawErr, ok := c.Expect["error"]; ok {
		wantErr, ok := rawErr.(map[string]interface{})
		switch {
		case !ok:
			problems = append(problems, "expect.error is not an object")
		case res.Error == nil:
			problems = append(problems, "expected an error; the call reported CHS_OK")
		default:
			problems = append(problems, matchCallError(wantErr, res.Error)...)
		}
	} else if res.Error != nil {
		problems = append(problems, fmt.Sprintf("unexpected error: %v", res.Error))
	}

	if rawOutputs, ok := c.Expect["outputs"]; ok {
		wantOutputs, ok := rawOutputs.(map[string]interface{})
		if !ok {
			problems = append(problems, "expect.outputs is not an object")
		} else {
			for name, wantDoc := range wantOutputs {
				gotDoc, present := res.Outputs[name]
				if !present {
					problems = append(problems, fmt.Sprintf("outputs[%s]: missing", name))
					continue
				}
				if err := matchExpected(wantDoc, gotDoc); err != nil {
					problems = append(problems, fmt.Sprintf("outputs[%s]: %v", name, err))
				}
			}
		}
	}

	if len(problems) > 0 {
		return false, strings.Join(problems, "; ")
	}
	return true, ""
}

func matchCallError(want map[string]interface{}, got *CallError) []string {
	var problems []string
	if wantCode, ok := want["ch_code"].(json.Number); ok {
		wc, err := wantCode.Int64()
		if err != nil {
			problems = append(problems, fmt.Sprintf("expect.error.ch_code: %v", err))
		} else if int64(got.ChCode) != wc {
			problems = append(problems, fmt.Sprintf("ch_code = %d, want %d", got.ChCode, wc))
		}
	}
	if wantName, ok := want["ch_name"].(string); ok && got.ChName != wantName {
		problems = append(problems, fmt.Sprintf("ch_name = %q, want %q", got.ChName, wantName))
	}
	if wantMsg, ok := want["message"].(string); ok && got.Message != wantMsg {
		problems = append(problems, fmt.Sprintf("message = %q, want %q", got.Message, wantMsg))
	}
	if wantCol, ok := want["column"].(string); ok && got.Column != wantCol {
		problems = append(problems, fmt.Sprintf("column = %q, want %q", got.Column, wantCol))
	}
	return problems
}

// matchExpected is a SUBSET match: every key expected must be present in
// actual and match (recursively); actual may carry additional keys expected
// does not name. That is deliberate, not sloppy -- scripts/abi-v1/emit/cases.py
// never predicts a minted handle's serial "id" (its own docstring: "a serial
// number no case can predict"), so an echoed handle argument always carries
// one more field than its cases.json expectation names.
func matchExpected(expected, actual interface{}) error {
	switch ev := expected.(type) {
	case map[string]interface{}:
		av, ok := actual.(map[string]interface{})
		if !ok {
			return fmt.Errorf("expected an object, got %T (%v)", actual, actual)
		}
		for k, evv := range ev {
			avv, present := av[k]
			if !present {
				return fmt.Errorf("%s: missing", k)
			}
			if err := matchExpected(evv, avv); err != nil {
				return fmt.Errorf("%s.%w", k, err)
			}
		}
		return nil
	case []interface{}:
		av, ok := actual.([]interface{})
		if !ok {
			return fmt.Errorf("expected an array, got %T", actual)
		}
		if len(av) != len(ev) {
			return fmt.Errorf("array length %d, want %d", len(av), len(ev))
		}
		for i := range ev {
			if err := matchExpected(ev[i], av[i]); err != nil {
				return fmt.Errorf("[%d]: %w", i, err)
			}
		}
		return nil
	default:
		if !jsonScalarEqual(expected, actual) {
			return fmt.Errorf("got %v (%T), want %v (%T)", actual, actual, expected, expected)
		}
		return nil
	}
}

// runLoaderCase loads the named stub variant under its own predicate and
// compares the refusal reason (or "accepted") against the case's
// expectation. The one case whose correct answer differs by platform
// (loader.ctor-marker.{linux,darwin}: D3's glibc step is linux-only, so the
// same impossible glibc_floor refuses on linux but loads fine on darwin) is
// now TWO cases, each carrying its own "os" and already-correct "expect" --
// the caller (TestConformance) skips whichever one does not match this leg
// before this function is ever reached, so nothing platform-specific is
// decided here.
func runLoaderCase(stubsDir string, manifest stubsManifest, c caseJSON) (bool, string) {
	variant, ok := manifest.Variants[c.Variant]
	if !ok {
		return false, fmt.Sprintf("stubs.json has no variant %q", c.Variant)
	}
	predJSON, err := json.Marshal(variant.Predicate)
	if err != nil {
		return false, fmt.Sprintf("marshaling the predicate: %v", err)
	}
	_, lerr := Load(LoadInput{
		LibraryPath: stubLibraryPath(stubsDir, variant),
		Predicate:   predJSON,
		Platform:    fmt.Sprintf("%s-%s", runtime.GOOS, runtime.GOARCH),
	})
	got := "accepted"
	if lerr != nil {
		got = lerr.Reason
	}

	want, _ := c.Expect["reason"].(string)
	if got != want {
		return false, fmt.Sprintf("Load(%s) reason = %q, want %q", c.Variant, got, want)
	}
	return true, ""
}
