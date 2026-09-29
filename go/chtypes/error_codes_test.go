package chtypes

// Revision 6's error-code table, in two halves.
//
// The first half needs no artifact: it parses documents in the shape
// chs_error_codes returns through the SAME errorCodeTableOf the Library path
// uses, and drives the success-only cache with a stand-in fetch. What it pins
// is the binding's own contract — unknown is absent, All is ascending, names
// match exactly, a NULL answer is never remembered — and none of it is a
// ClickHouse fact.
//
// The second half asks a loaded revision-6 library and skips LOUDLY, by name,
// without one. The names it expects are what the header documents for those
// lines, stated as expectations about the artifact rather than as a table: a
// disagreement is a finding about the build.

import (
	"errors"
	"path/filepath"
	"reflect"
	"testing"
)

// A fake document: out of order on purpose, with an unknown key at both
// levels, an entry with no name, and a negative code — none of which may
// reach a lookup.
const fakeErrorCodesDoc = `{
  "error_codes": [
    {"code": 252, "name": "TOO_MANY_PARTS", "since": "whatever"},
    {"code": 0, "name": "OK"},
    {"code": 1, "name": "UNSUPPORTED_METHOD"},
    {"code": 7, "name": ""},
    {"code": -2, "name": "NOT_A_CLICKHOUSE_CODE"},
    {"code": 47, "name": "UNKNOWN_IDENTIFIER"}
  ],
  "generator": "ignored"
}`

func TestErrorCodeTableLookups(t *testing.T) {
	tbl, err := errorCodeTableOf([]byte(fakeErrorCodesDoc))
	if err != nil {
		t.Fatalf("errorCodeTableOf: %v", err)
	}
	for code, want := range map[int]string{0: "OK", 1: "UNSUPPORTED_METHOD", 47: "UNKNOWN_IDENTIFIER", 252: "TOO_MANY_PARTS"} {
		got, ok := tbl.Name(code)
		if !ok || got != want {
			t.Errorf("Name(%d) = %q, %v; want %q, true", code, got, ok, want)
		}
		back, ok := tbl.Code(want)
		if !ok || back != code {
			t.Errorf("Code(%q) = %d, %v; want %d, true", want, back, ok, code)
		}
	}
	// Absent, never synthesized: an unknown code, the entry with no name, a
	// negative code even though the document carried one, and the ABI's own
	// sentinels.
	for _, code := range []int{2, 7, 999999, -1, -2, -3} {
		if got, ok := tbl.Name(code); ok {
			t.Errorf("Name(%d) = %q, true; want absent", code, got)
		}
	}
	// Exact and case-sensitive; no trimming, no folding.
	for _, name := range []string{"too_many_parts", "Too_Many_Parts", " TOO_MANY_PARTS", "TOO_MANY_PARTS ", "", "NOT_A_CLICKHOUSE_CODE", "NO_SUCH_ERROR"} {
		if got, ok := tbl.Code(name); ok {
			t.Errorf("Code(%q) = %d, true; want absent", name, got)
		}
	}
}

func TestErrorCodeTableAllIsAscendingAndACopy(t *testing.T) {
	tbl, err := errorCodeTableOf([]byte(fakeErrorCodesDoc))
	if err != nil {
		t.Fatalf("errorCodeTableOf: %v", err)
	}
	want := []ErrorCodeEntry{{0, "OK"}, {1, "UNSUPPORTED_METHOD"}, {47, "UNKNOWN_IDENTIFIER"}, {252, "TOO_MANY_PARTS"}}
	all := tbl.All()
	if !reflect.DeepEqual(all, want) {
		t.Fatalf("All() = %v, want %v", all, want)
	}
	all[0].Name = "CHANGED"
	if got, _ := tbl.Name(0); got != "OK" {
		t.Fatalf("mutating All()'s slice changed the table: Name(0) = %q", got)
	}
}

func TestErrorCodeTableFirstEntryWinsOnARepeat(t *testing.T) {
	tbl, err := errorCodeTableOf([]byte(`{"error_codes":[{"code":5,"name":"A_NAME"},{"code":5,"name":"B_NAME"},{"code":6,"name":"A_NAME"}]}`))
	if err != nil {
		t.Fatalf("errorCodeTableOf: %v", err)
	}
	if got, _ := tbl.Name(5); got != "A_NAME" {
		t.Errorf("Name(5) = %q, want the first entry's A_NAME", got)
	}
	if _, ok := tbl.Name(6); ok {
		t.Errorf("code 6 repeats a name already taken and must not be answered")
	}
	if n := len(tbl.All()); n != 1 {
		t.Errorf("All() has %d entries, want 1", n)
	}
}

func TestErrorCodeTableAbsentKeysAreEmpty(t *testing.T) {
	for _, doc := range []string{`{}`, `{"error_codes":null}`, `{"error_codes":[]}`, `{"something_else":[1,2,3]}`} {
		tbl, err := errorCodeTableOf([]byte(doc))
		if err != nil {
			t.Fatalf("%s: %v", doc, err)
		}
		if n := len(tbl.All()); n != 0 {
			t.Errorf("%s: All() has %d entries, want 0", doc, n)
		}
		if _, ok := tbl.Name(0); ok {
			t.Errorf("%s: Name(0) answered from an empty table", doc)
		}
	}
}

// The same list every binding's bad-document test runs: a truncated document,
// a top-level value that is not an object, error_codes that is not an array,
// an entry that is not an object, and a field of the wrong type. Each is the
// plain error — never a refusal, never a decline — and never an empty table.
func TestErrorCodeTableRefusesABadDocument(t *testing.T) {
	for _, doc := range []string{
		`{"error_codes":[`,
		`[]`,
		`null`,
		`42`,
		`"x"`,
		`{"error_codes":{}}`,
		`{"error_codes":[null]}`,
		`{"error_codes":[[252,"X"]]}`,
		`{"error_codes":[{"code":"252","name":"X"}]}`,
		`{"error_codes":[{"code":252.5,"name":"X"}]}`,
		`{"error_codes":[{"code":1,"name":5}]}`,
	} {
		tbl, err := errorCodeTableOf([]byte(doc))
		if err == nil {
			t.Errorf("%s: parsed to a table of %d entries; want an error", doc, len(tbl.All()))
			continue
		}
		var ue *UnsupportedError
		var se *SchemaError
		if errors.As(err, &ue) || errors.As(err, &se) {
			t.Errorf("%s: %v is a refusal or a decline; a bad document is neither", doc, err)
		}
	}
}

// The cache keeps a table that was built and nothing else: a NULL answer (the
// guarded-exception case) and a decline both come back as errors, and the
// next call asks again.
func TestErrorCodeCacheKeepsSuccessOnly(t *testing.T) {
	var c errorCodeCache
	calls := 0
	nullOnce := func() (*ErrorCodeTable, error) { calls++; return nil, errNoErrorCodesDocument }
	decline := func() (*ErrorCodeTable, error) {
		calls++
		return nil, &UnsupportedError{Msg: "this artifact predates chs_error_codes (rebuild it)"}
	}
	build := func() (*ErrorCodeTable, error) { calls++; return errorCodeTableOf([]byte(fakeErrorCodesDoc)) }

	if _, err := c.get(nullOnce); !errors.Is(err, errNoErrorCodesDocument) {
		t.Fatalf("a NULL answer: err = %v", err)
	}
	var ue *UnsupportedError
	if _, err := c.get(decline); !errors.As(err, &ue) {
		t.Fatalf("a missing symbol: err = %v, want *UnsupportedError", err)
	}
	first, err := c.get(build)
	if err != nil || first == nil {
		t.Fatalf("a built table: %v, %v", first, err)
	}
	again, err := c.get(func() (*ErrorCodeTable, error) {
		t.Fatal("the cache asked the library again after a table was built")
		return nil, nil
	})
	if err != nil || again != first {
		t.Fatalf("second call = %p, %v; want the cached %p", again, err, first)
	}
	if calls != 3 {
		t.Fatalf("fetch ran %d times, want 3 (two failures, one build)", calls)
	}
}

// ------------------------------------------------- through the ABI fixture

// TestErrorCodesThroughTheABIFixture drives chs_error_codes through the REAL
// dlopen path, on the at-revision stub the ABI fixture builds from THIS
// header — the one library that exists before any revision-6 artifact does,
// and the one every pull request's abi-fixtures job loads. The stub answers
// by return type, not with a table, so this pins the wiring rather than an
// answer: the symbol resolves (never the decline type, and never a refusal),
// and whatever it answers takes the rule — a NULL is the plain no-document
// error and is not kept, so the second call asks again; a document is kept.
func TestErrorCodesThroughTheABIFixture(t *testing.T) {
	doc := loadABIRevisionFixture(t)
	at := filepath.Join(doc.Root, "at-revision")
	r, err := NewRegistry(at)
	if err != nil {
		t.Fatalf("NewRegistry(%s): %v", at, err)
	}
	lib, err := r.For(Version(doc.ClickHouseMinor))
	if err != nil {
		t.Fatalf("Registry.For(%q) on %s: %v", doc.ClickHouseMinor, at, err)
	}
	first, err := lib.ErrorCodes()
	var ue *UnsupportedError
	var se *SchemaError
	if errors.As(err, &ue) || errors.As(err, &se) {
		t.Fatalf("ErrorCodes() on a stub built from this header = %v; the symbol is declared there, "+
			"so neither a decline nor a refusal is a possible answer", err)
	}
	second, err2 := lib.ErrorCodes()
	if err == nil {
		if err2 != nil || second != first {
			t.Fatalf("a built table was not kept: second call = %p, %v; first = %p", second, err2, first)
		}
		t.Logf("the stub answered a document of %d entries; kept", len(first.All()))
		return
	}
	t.Logf("the stub answered NULL through the real dlopen path: %v", err)
	if !errors.Is(err, errNoErrorCodesDocument) {
		t.Fatalf("a NULL answer must be the plain no-document error, got %v", err)
	}
	if !errors.Is(err2, errNoErrorCodesDocument) {
		t.Fatalf("after a NULL answer the next call must ask the library again, got %v", err2)
	}
}

// ------------------------------------------------------------ with artifacts

// rev6Libraries opens every line of the test registry that loads at ABI
// revision 6, and skips LOUDLY, by name, when there is none — the registry
// default for this package is the revision-6 directory, which is empty until
// the artifact producer publishes revision-6 artifacts.
func rev6Libraries(t *testing.T) []*Library {
	t.Helper()
	dir := testRegistryDir(t) // skips by name when the registry is empty/absent
	reg, err := NewRegistry(dir)
	if err != nil {
		t.Fatalf("registry %s did not open: %v", dir, err)
	}
	var libs []*Library
	for _, v := range reg.Versions() {
		lib, err := reg.For(Version(v))
		if err != nil {
			t.Logf("line %s not exercised: %v", v, err)
			continue
		}
		if lib.ABIRevision < 6 {
			t.Logf("line %s not exercised: artifact reports ABI revision %d, these cases need 6", v, lib.ABIRevision)
			continue
		}
		libs = append(libs, lib)
	}
	if len(libs) == 0 {
		t.Skipf("registry %s holds no ABI revision-6 artifact: every case here needs one — fetch one with scripts/fetch.sh (docs/guides/fetch.md)", dir)
	}
	return libs
}

func TestErrorCodesFromTheLoadedLibrary(t *testing.T) {
	libs := rev6Libraries(t)
	for _, lib := range libs {
		tbl, err := lib.ErrorCodes()
		if err != nil {
			t.Fatalf("%s: ErrorCodes: %v", lib.Minor, err)
		}
		all := tbl.All()
		if len(all) == 0 {
			t.Fatalf("%s: the table is empty", lib.Minor)
		}
		for i := 1; i < len(all); i++ {
			if all[i-1].Code >= all[i].Code {
				t.Fatalf("%s: All() is not strictly ascending at %d: %v then %v", lib.Minor, i, all[i-1], all[i])
			}
		}
		// Every entry round-trips through both lookups.
		for _, e := range all {
			if n, ok := tbl.Name(e.Code); !ok || n != e.Name {
				t.Errorf("%s: Name(%d) = %q, %v; All() says %q", lib.Minor, e.Code, n, ok, e.Name)
			}
			if c, ok := tbl.Code(e.Name); !ok || c != e.Code {
				t.Errorf("%s: Code(%q) = %d, %v; All() says %d", lib.Minor, e.Name, c, ok, e.Code)
			}
		}
		// The code a batch-shape refusal carries, named by the build itself.
		if n, ok := tbl.Name(252); !ok || n != "TOO_MANY_PARTS" {
			t.Errorf("%s: Name(252) = %q, %v; want TOO_MANY_PARTS", lib.Minor, n, ok)
		}
		// The ABI's sentinels are never ClickHouse codes.
		for _, code := range []int{-1, -2} {
			if n, ok := tbl.Name(code); ok {
				t.Errorf("%s: Name(%d) = %q; a sentinel must be absent", lib.Minor, code, n)
			}
		}
		// The second call is the same table: built once, kept on success.
		again, err := lib.ErrorCodes()
		if err != nil || again != tbl {
			t.Errorf("%s: a second ErrorCodes() call returned %p, %v; want the kept %p", lib.Minor, again, err, tbl)
		}
	}
}

// 903 is the header's own example of one number naming two errors on two
// lines. Checked per line, for whichever of those lines the registry holds.
func TestErrorCode903DiffersAcrossLines(t *testing.T) {
	libs := rev6Libraries(t)
	want := map[string]string{
		"25.3": "LICENSE_EXPIRED",
		"25.8": "LICENSE_EXPIRED",
	}
	checked := 0
	for _, lib := range libs {
		expected, known := want[lib.Minor]
		if !known {
			if minorSortKey("26.2").before(minorSortKey(lib.Minor)) || lib.Minor == "26.2" {
				expected, known = "DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN", true
			}
		}
		if !known {
			t.Logf("%s: no documented expectation for code 903; not checked", lib.Minor)
			continue
		}
		tbl, err := lib.ErrorCodes()
		if err != nil {
			t.Fatalf("%s: ErrorCodes: %v", lib.Minor, err)
		}
		if got, ok := tbl.Name(903); !ok || got != expected {
			t.Errorf("%s: Name(903) = %q, %v; the header documents %q for this line", lib.Minor, got, ok, expected)
		}
		checked++
	}
	if checked == 0 {
		t.Skip("no loaded revision-6 line has a documented expectation for code 903 (25.3, 25.8, or 26.2 on)")
	}
}
