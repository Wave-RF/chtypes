package chtypes

// stub_test.go — the public API driven end to end over the ABI v2 test stub
// (scripts/abi-v1/build-stubs.sh): a real tiny implementation of D2's handle
// rules and D3's status shape that echoes its inputs. It proves the plumbing
// (one call per operation, bytes both ways, class mapping, close guards,
// finalizers, concurrency); what ClickHouse says is the artifact's to prove.
// Without CHTYPES_ABI2_STUBS every test here skips LOUDLY by name.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
)

func stubDir(t *testing.T) string {
	t.Helper()
	dir := os.Getenv("CHTYPES_ABI2_STUBS")
	if dir == "" {
		t.Skip("SKIPPED: CHTYPES_ABI2_STUBS is not set (scripts/abi-v1/build-stubs.sh --out DIR); the stub-driven public API tests did not run")
	}
	return dir
}

// stubFile is a stub variant's library, copied to its own path so each test
// that needs a fresh image (one that observes load step 7) gets one.
func stubFile(t *testing.T, variant string) string {
	t.Helper()
	dir := stubDir(t)
	src := filepath.Join(dir, variant+".so")
	b, err := os.ReadFile(src)
	if err != nil {
		t.Fatalf("reading the %s stub: %v", variant, err)
	}
	dst := filepath.Join(t.TempDir(), "libstub-"+variant+".so")
	if err := os.WriteFile(dst, b, 0o755); err != nil {
		t.Fatal(err)
	}
	return dst
}

func openStub(t *testing.T, variant string) *Library {
	t.Helper()
	resetSetup(t)
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
	lib, err := OpenUnverified(stubFile(t, variant), true)
	if err != nil {
		t.Fatalf("opening the %s stub: %v", variant, err)
	}
	return lib
}

type echo struct {
	Fn   string `json:"fn"`
	Out  string `json:"out"`
	Args []struct {
		Len    int    `json:"len"`
		SHA256 string `json:"sha256"`
	} `json:"args"`
}

func parseEcho(t *testing.T, s string) echo {
	t.Helper()
	var e echo
	if err := json.Unmarshal([]byte(s), &e); err != nil {
		t.Fatalf("the stub's echo %q: %v", s, err)
	}
	return e
}

func TestStubOpenAndBuildInfo(t *testing.T) {
	lib := openStub(t, "ok")
	bi := lib.BuildInfo()
	if lib.Version == "" || lib.Version != bi.ClickHouseVersion || lib.Minor != bi.ClickHouseMinor || lib.Path == "" {
		t.Errorf("library = %+v, build_info = %+v: Version and Minor are build_info's fields, read not derived", lib, bi)
	}
	if bi.ABI != abi2.ChsAbiVersion || bi.ABIFingerprint != abi2.ChsAbiFingerprint || bi.Schema != 1 || string(bi.Raw) == "" || lib.Resolved() != nil {
		t.Errorf("build_info = %+v, resolved = %v", bi, lib.Resolved())
	}
}

func TestStubUnverifiedNeedsBothOptIns(t *testing.T) {
	resetSetup(t)
	path := stubFile(t, "ok")
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "")
	var ue *UsageError
	if _, err := OpenUnverified(path, true); !errors.As(err, &ue) {
		t.Errorf("without the environment opt-in = %v, want a *UsageError", err)
	}
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
	if _, err := OpenUnverified(path, false); !errors.As(err, &ue) {
		t.Errorf("without allow = %v, want a *UsageError", err)
	}
}

func TestStubBytesBothWays(t *testing.T) {
	lib := openStub(t, "ok")
	// A byte string is never a C string and never assumed UTF-8: NUL and
	// invalid UTF-8 round-trip through length and hash.
	for _, in := range []string{"UInt8", "a\x00b", "\xff\xfe\x00", "", strings.Repeat("x", 10000)} {
		out, err := lib.ValidateType(in)
		if err != nil {
			t.Fatalf("ValidateType(%q): %v", in, err)
		}
		e := parseEcho(t, out)
		sum := sha256.Sum256([]byte(in))
		if e.Fn != "chs_type_validate" || len(e.Args) != 1 || e.Args[0].Len != len(in) || (len(in) > 0 && e.Args[0].SHA256 != hex.EncodeToString(sum[:])) {
			t.Errorf("input %q echoed as %+v", in, e)
		}
	}
	for name, call := range map[string]func(string) (string, error){
		"chs_back_quote":           lib.QuoteIdentifier,
		"chs_back_quote_if_needed": lib.QuoteIdentifierIfNeeded,
		"chs_quote_string":         lib.QuoteLiteral,
	} {
		out, err := call("a\x00b")
		if err != nil || parseEcho(t, out).Fn != name {
			t.Errorf("%s: %q, %v", name, out, err)
		}
	}
	q, err := lib.DiscoverQuery()
	if err != nil || parseEcho(t, q).Fn != "chs_discover_query" {
		t.Errorf("DiscoverQuery = %q, %v", q, err)
	}
}

func TestStubStatusInjectionMapsByTheD3Table(t *testing.T) {
	lib := openStub(t, "ok")
	cases := []struct {
		status string
		check  func(error) bool
	}{
		{"CHS_REJECTED", func(e error) bool { var x *SchemaError; return errors.As(e, &x) }},
		{"CHS_DECLINED", func(e error) bool { var x *UnsupportedError; return errors.As(e, &x) }},
		{"CHS_INVALID_ARGUMENT", func(e error) bool { var x *UsageError; return errors.As(e, &x) }},
		{"CHS_INTERNAL", func(e error) bool { var x *InternalError; return errors.As(e, &x) }},
	}
	for _, c := range cases {
		_, err := lib.ValidateType("!S:" + c.status + ":62:SYNTAX_ERROR:the message: with a colon")
		if !c.check(err) {
			t.Errorf("%s -> %T", c.status, err)
			continue
		}
		ce, _ := AsCallError(err)
		if ce.ChCode != 62 || ce.ChName != "SYNTAX_ERROR" || ce.Message != "the message: with a colon" {
			t.Errorf("%s: fields = %+v", c.status, ce)
		}
	}
}

func TestStubSchemaFilterBlockLifecycle(t *testing.T) {
	lib := openStub(t, "ok")
	schema, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory",
		WithSettings(map[string]string{"a": "b"}), WithSessionTimezone("UTC"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := schema.Describe(); err != nil {
		t.Errorf("Describe: %v", err)
	}
	if _, err := schema.Row(JSONEachRow, []byte(`{"x":1}`), WithColumns([]string{"x"}), WithSessionTimezone("UTC")); err != nil {
		t.Errorf("Row: %v", err)
	}
	f, err := schema.CompileFilter("x > 1", WithFilterParams(map[string]string{"p": "1"}), WithSessionTimezone("UTC"))
	if err != nil {
		t.Fatal(err)
	}
	blk, err := schema.ParseBlock(CSV, []byte("1\n"), WithColumns([]string{"x"}))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Eval(blk); err != nil {
		t.Errorf("Eval: %v", err)
	}
	if _, err := f.Rows(JSONEachRow, []byte(`{"x":2}`), WithSettings(map[string]string{"a": "b"})); err != nil {
		t.Errorf("Filter.Rows: %v", err)
	}
	if _, err := schema.Rows(JSONEachRow, []byte(`{"x":2}`), WithRowFilter(f), WithExport(CSV), WithDocFlags(DocValues)); err != nil {
		t.Errorf("Rows: %v", err)
	}
	// The zone given twice is refused before any call.
	_, err = schema.Row(JSONEachRow, nil, WithSettings(map[string]string{"session_timezone": "UTC"}), WithSessionTimezone("UTC"))
	var ue *UsageError
	if !errors.As(err, &ue) {
		t.Errorf("zone twice = %v, want a *UsageError", err)
	}

	// Close is idempotent and any order is safe: the schema first, while its
	// filter and block are in use, and they keep working.
	if err := schema.Close(); err != nil {
		t.Fatal(err)
	}
	if err := schema.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := f.Eval(blk); err != nil {
		t.Errorf("a filter must keep working after its schema closes: %v", err)
	}
	// Use after close is a UsageError, raised before any C call.
	checks := map[string]func() error{
		"Describe":      func() error { _, e := schema.Describe(); return e },
		"Row":           func() error { _, e := schema.Row(JSONEachRow, nil); return e },
		"Rows":          func() error { _, e := schema.Rows(JSONEachRow, nil); return e },
		"CompileFilter": func() error { _, e := schema.CompileFilter("1"); return e },
		"ParseBlock":    func() error { _, e := schema.ParseBlock(CSV, nil); return e },
	}
	for name, call := range checks {
		if err := call(); !errors.As(err, &ue) || !strings.Contains(err.Error(), "closed") {
			t.Errorf("%s after Close = %v, want a *UsageError naming the closed schema", name, err)
		}
	}
	_ = blk.Close()
	_ = f.Close()
	if _, err := f.Eval(blk); !errors.As(err, &ue) {
		t.Errorf("Eval over closed handles = %v", err)
	}
	if _, err := f.Rows(CSV, nil); !errors.As(err, &ue) {
		t.Errorf("Filter.Rows after Close = %v", err)
	}
	var nilSchema *Schema
	if _, err := nilSchema.Row(CSV, nil); !errors.As(err, &ue) {
		t.Errorf("a nil schema = %v", err)
	}
	if nilSchema.Close() != nil {
		t.Error("closing a nil schema is a no-op")
	}
}

func TestStubSchemaRefusalFromCompile(t *testing.T) {
	lib := openStub(t, "ok")
	_, err := lib.CompileTable("!S:CHS_REJECTED:50:UNKNOWN_TYPE:no such type")
	var se *SchemaError
	if !errors.As(err, &se) || se.ChCode != 50 {
		t.Errorf("CompileTable = %v, want the refusal verbatim", err)
	}
}

func TestStubCrossLibraryHandleIsTheLibrarysToRefuse(t *testing.T) {
	a, b := openStub(t, "ok"), openStub(t, "ok-b")
	sa, err := a.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory")
	if err != nil {
		t.Fatal(err)
	}
	sb, err := b.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory")
	if err != nil {
		t.Fatal(err)
	}
	fb, err := sb.CompileFilter("x > 1")
	if err != nil {
		t.Fatal(err)
	}
	_, err = sa.Rows(JSONEachRow, []byte(`{"x":1}`), WithRowFilter(fb))
	var ue *UsageError
	if !errors.As(err, &ue) || ue.Status != StatusInvalidArgument {
		t.Errorf("a filter from another image = %v, want the library's INVALID_ARGUMENT as a *UsageError", err)
	}
}

func TestStubStep7FailureIsTheCallsOwnError(t *testing.T) {
	cases := []struct {
		zone  string
		check func(error) bool
	}{
		// INVALID_ARGUMENT from chs_initialize: a UsageError, never a refusal.
		{"!S:CHS_INVALID_ARGUMENT:0::zone Europe/Paris vs UTC", func(e error) bool { var x *UsageError; return errors.As(e, &x) }},
		// A zone DateLUT will not load: a SchemaError carrying its own code.
		{"!S:CHS_REJECTED:1000:BAD_ARGUMENTS:no such zone", func(e error) bool {
			var x *SchemaError
			return errors.As(e, &x) && x.ChCode == 1000
		}},
	}
	for _, c := range cases {
		resetSetup(t)
		t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
		if err := Setup(SetupOptions{Timezone: c.zone}); err != nil {
			t.Fatal(err)
		}
		_, err := OpenUnverified(stubFile(t, "ok"), true)
		if !c.check(err) {
			t.Errorf("step 7 with %q = %T %v", c.zone, err, err)
		}
		var ae *ArtifactError
		if errors.As(err, &ae) {
			t.Errorf("a step 7 failure is not a refusal reason: %v", err)
		}
	}
}

func TestStubDefaultsAreAppliedAtStep7(t *testing.T) {
	resetSetup(t)
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
	if err := Setup(SetupOptions{Timezone: "UTC", Defaults: map[string]string{"a": "b"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenUnverified(stubFile(t, "ok"), true); err != nil {
		t.Errorf("an open under a setup with defaults: %v", err)
	}
}

func TestStubErrorCodesAndLiveHandles(t *testing.T) {
	lib := openStub(t, "ok")
	h, err := lib.LiveHandles()
	if err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"chs_schema", "chs_filter", "chs_block"} {
		if _, ok := h[kind]; !ok {
			t.Errorf("LiveHandles lacks %s: %v", kind, h)
		}
	}
	// The stub's chs_error_codes answers its echo, not a table: a document
	// that does not decode is an InternalError, and a failure is never cached.
	_, err = lib.ErrorCodes()
	var ie *InternalError
	if !errors.As(err, &ie) {
		t.Errorf("a document that is not the table = %v, want an *InternalError", err)
	}
}

// TestStubZeroLiveHandles abandons handles and requires every live count to
// reach zero: the finalizers are the proof, through live_handles.
func TestStubZeroLiveHandles(t *testing.T) {
	lib := openStub(t, "ok")
	// The counter works: a handle held open is counted.
	held, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory")
	if err != nil {
		t.Fatal(err)
	}
	if h, err := lib.LiveHandles(); err != nil || h["chs_schema"] != 1 {
		t.Fatalf("a held schema must be counted live: %v, %v", h, err)
	}
	if err := held.Close(); err != nil {
		t.Fatal(err)
	}
	func() {
		for i := 0; i < 50; i++ {
			s, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory")
			if err != nil {
				t.Fatal(err)
			}
			f, err := s.CompileFilter("x > 1")
			if err != nil {
				t.Fatal(err)
			}
			b, err := s.ParseBlock(CSV, []byte("1\n"))
			if err != nil {
				t.Fatal(err)
			}
			srv, err := lib.NewServer(ServerProfile{Timezone: "UTC"})
			if err != nil {
				t.Fatal(err)
			}
			on, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory", OnServer(srv))
			if err != nil {
				t.Fatal(err)
			}
			_, _, _, _, _ = s, f, b, srv, on // abandoned, never closed
		}
	}()
	deadline := time.Now().Add(20 * time.Second)
	for {
		runtime.GC()
		time.Sleep(20 * time.Millisecond)
		h, err := lib.LiveHandles()
		if err != nil {
			t.Fatal(err)
		}
		var live uint64
		for _, n := range h {
			live += n
		}
		if live == 0 {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("handles still live after abandoning them: %v", h)
		}
	}
}

// TestStubConcurrentCallsAndClose hammers one handle from many goroutines
// while another closes it: no call takes a lock beyond the close guard, every
// call before Close succeeds, every call after is a UsageError, and nothing
// crashes (run under -race).
func TestStubConcurrentCallsAndClose(t *testing.T) {
	lib := openStub(t, "ok")
	schema, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory")
	if err != nil {
		t.Fatal(err)
	}
	f, err := schema.CompileFilter("x > 1")
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	stop := make(chan struct{})
	var bad error
	var badMu sync.Mutex
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; ; i++ {
				select {
				case <-stop:
					return
				default:
				}
				_, err := schema.Row(JSONEachRow, []byte(`{"x":1}`))
				if err == nil {
					_, err = f.Rows(JSONEachRow, []byte(`{"x":1}`))
				}
				var ue *UsageError
				if err != nil && !errors.As(err, &ue) {
					badMu.Lock()
					bad = err
					badMu.Unlock()
					return
				}
			}
		}()
	}
	time.Sleep(50 * time.Millisecond)
	_ = schema.Close()
	_ = f.Close()
	time.Sleep(20 * time.Millisecond)
	close(stop)
	wg.Wait()
	if bad != nil {
		t.Errorf("a call failed with something other than a close-guard UsageError: %v", bad)
	}
}

// TestStubRealDocuments drives the decoders with the documents the stub itself
// builds on a `!D:` body (tests/fixtures/abi-v2/cases.json: document.*): not
// the echo, and not a hand-written shape.
func TestStubRealDocuments(t *testing.T) {
	lib := openStub(t, "ok")
	schema, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	// document.preview_row.binary_string: invalid UTF-8 and NUL in a String.
	r, err := schema.Row(JSONEachRow, []byte("!D:\xff\x00\x80"))
	if err != nil {
		t.Fatal(err)
	}
	if r.Outcome != Accepted || len(r.Columns) != 1 {
		t.Fatalf("row = %+v", r)
	}
	c := r.Columns[0]
	if c.Column != "s" || c.Text != "\xff\x00\x80" || c.Value == nil || *c.Value != "\xff\x00\x80" || c.Source != SourceInput || !c.IsStored || c.Null {
		t.Errorf("binary String = %+v", c)
	}
	if r.InputSpan == nil || r.InputSpan.Len != 6 {
		t.Errorf("InputSpan = %v", r.InputSpan)
	}
	// document.preview_row.utf8_with_nul: a NUL in valid UTF-8 travels as text.
	r, err = schema.Row(JSONEachRow, []byte("!D:a\x00b"))
	if err != nil {
		t.Fatal(err)
	}
	if c := r.Columns[0]; c.Text != "a\x00b" || c.Value == nil || *c.Value != "a\x00b" {
		t.Errorf("UTF-8 with NUL = %+v", c)
	}
	// document.discover_columns.binary_name: a column name that is not UTF-8.
	d, err := lib.DiscoverColumns([]byte("!D:\xffcol"))
	if err != nil {
		t.Fatal(err)
	}
	if len(d.Columns) != 1 || d.Columns[0].Name != "\xffcol" || d.Columns[0].Declaration != "\xffcol String" || d.ColumnsSQL != "\xffcol String" {
		t.Errorf("discovery = %+v", d)
	}
}
