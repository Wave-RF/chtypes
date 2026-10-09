package chtypes

// server_test.go — the server profile (NewServer, OnServer, Server.Close and
// the description's server and replicated members). The profile document is
// checked byte for byte with no library; everything else runs over the ABI v2
// test stub (stub_test.go), whose chs_schema_create takes a counted reference
// to the server it RECEIVES, so the stub's own live_handles counters say
// whether a non-NULL server reached it. Without CHTYPES_ABI2_STUBS the stub
// tests skip loudly by name.

import (
	"errors"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestServerProfileJSON pins the profile document byte for byte: a zero
// field is omitted, a nil map is omitted, and a non-nil map is sent, as {}
// when it is empty. Nothing is validated or rewritten.
func TestServerProfileJSON(t *testing.T) {
	cases := []struct {
		name string
		p    ServerProfile
		want string
	}{
		{"zero: nothing described", ServerProfile{}, `{}`},
		{"timezone", ServerProfile{Timezone: "Asia/Tokyo"}, `{"timezone":"Asia/Tokyo"}`},
		{"nil settings omitted", ServerProfile{Timezone: "UTC", Settings: nil}, `{"timezone":"UTC"}`},
		{"empty settings sent", ServerProfile{Settings: map[string]string{}}, `{"settings":{}}`},
		{"nil macros omitted (unknown)", ServerProfile{Macros: nil}, `{}`},
		{"empty macros sent (the complete set is empty)", ServerProfile{Macros: map[string]string{}}, `{"macros":{}}`},
		{
			"every member, keys sorted",
			ServerProfile{
				Timezone: "Europe/Berlin",
				Settings: map[string]string{"max_threads": "8", "date_time_input_format": "best_effort"},
				Macros:   map[string]string{"shard": "01", "replica": "r1"},
			},
			`{"timezone":"Europe/Berlin","settings":{"date_time_input_format":"best_effort","max_threads":"8"},"macros":{"replica":"r1","shard":"01"}}`,
		},
		// Passed through as given: a zone DateLUT will not load, a setting no
		// server knows, a value no server parses and a macro name no reader
		// keeps are the library's to refuse, never the binding's.
		{
			"never validated",
			ServerProfile{
				Timezone: "Not/AZone",
				Settings: map[string]string{"no_such_setting": "eight"},
				Macros:   map[string]string{"": "x", "a.b": "<&>"},
			},
			`{"timezone":"Not/AZone","settings":{"no_such_setting":"eight"},"macros":{"":"x","a.b":"<&>"}}`,
		},
	}
	for _, c := range cases {
		got, err := serverProfileJSON(c.p)
		if err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		if string(got) != c.want {
			t.Errorf("%s:\n got %s\nwant %s", c.name, got, c.want)
		}
	}
}

// TestServerProfileMacrosAbsentIsNotEmpty fails if the normative distinction
// collapses: absent macros mean UNKNOWN, an empty set means the server has
// none (input:server_profile).
func TestServerProfileMacrosAbsentIsNotEmpty(t *testing.T) {
	absent, err := serverProfileJSON(ServerProfile{Timezone: "UTC"})
	if err != nil {
		t.Fatal(err)
	}
	empty, err := serverProfileJSON(ServerProfile{Timezone: "UTC", Macros: map[string]string{}})
	if err != nil {
		t.Fatal(err)
	}
	if string(absent) == string(empty) {
		t.Fatalf("nil and empty macros serialize alike (%s): absent (unknown) and {} (complete) must differ", absent)
	}
	if strings.Contains(string(absent), "macros") || !strings.Contains(string(empty), `"macros":{}`) {
		t.Errorf("absent = %s, empty = %s", absent, empty)
	}
}

// liveServers is the stub's own count of live chs_server handles.
func liveServers(t *testing.T, lib *Library) uint64 {
	t.Helper()
	h, err := lib.LiveHandles()
	if err != nil {
		t.Fatal(err)
	}
	n, ok := h["chs_server"]
	if !ok {
		t.Fatalf("live_handles has no chs_server: %v", h)
	}
	return n
}

// TestStubServerProfileIsTheDescriptionsDocument sends every member: the
// stub's closed-document check (rule r1), generated from the description's
// input:server_profile, refuses any key the description does not list, so a
// misspelled member fails here.
func TestStubServerProfileIsTheDescriptionsDocument(t *testing.T) {
	lib := openStub(t, "ok")
	srv, err := lib.NewServer(ServerProfile{
		Timezone: "Asia/Tokyo",
		Settings: map[string]string{"max_threads": "8"},
		Macros:   map[string]string{},
	})
	if err != nil {
		t.Fatalf("a profile with every member: %v", err)
	}
	_ = srv.Close()
}

func TestStubNewServerAndCloseTwice(t *testing.T) {
	lib := openStub(t, "ok")
	srv, err := lib.NewServer(ServerProfile{Timezone: "UTC"})
	if err != nil {
		t.Fatal(err)
	}
	if n := liveServers(t, lib); n != 1 {
		t.Fatalf("live chs_server after NewServer = %d, want 1", n)
	}
	if err := srv.Close(); err != nil {
		t.Fatal(err)
	}
	if err := srv.Close(); err != nil {
		t.Fatalf("a second Close: %v", err)
	}
	if n := liveServers(t, lib); n != 0 {
		t.Errorf("live chs_server after Close = %d, want 0", n)
	}
	var nilServer *Server
	if nilServer.Close() != nil {
		t.Error("closing a nil server is a no-op")
	}
}

// TestStubOnServerPassesTheHandle reads what the stub received: its
// chs_schema_create holds the server it was given, so after the caller closes
// the server, the stub still counts it live exactly when the schema got a
// non-NULL server, and frees it with the schema.
func TestStubOnServerPassesTheHandle(t *testing.T) {
	lib := openStub(t, "ok")
	const stmt = "CREATE TABLE t (k UInt8) ENGINE = Memory"

	srv, err := lib.NewServer(ServerProfile{Timezone: "Asia/Tokyo"})
	if err != nil {
		t.Fatal(err)
	}
	schema, err := lib.CompileTable(stmt, OnServer(srv), WithSettings(map[string]string{"a": "b"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := srv.Close(); err != nil {
		t.Fatal(err)
	}
	if n := liveServers(t, lib); n != 1 {
		t.Fatalf("live chs_server after closing the server under a schema = %d, want 1: the stub did not receive the server", n)
	}
	if _, err := schema.Describe(); err != nil {
		t.Errorf("a schema whose server the caller closed keeps working: %v", err)
	}
	if err := schema.Close(); err != nil {
		t.Fatal(err)
	}
	if n := liveServers(t, lib); n != 0 {
		t.Errorf("live chs_server after closing the schema = %d, want 0", n)
	}

	// The control: without OnServer, and with OnServer(nil), the stub receives
	// NULL and holds nothing.
	for name, opts := range map[string][]CompileOption{"no OnServer": nil, "OnServer(nil)": {OnServer(nil)}} {
		srv, err := lib.NewServer(ServerProfile{})
		if err != nil {
			t.Fatal(err)
		}
		plain, err := lib.CompileTable(stmt, opts...)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		_ = srv.Close()
		if n := liveServers(t, lib); n != 0 {
			t.Errorf("%s: live chs_server = %d, want 0: a schema with no server holds none", name, n)
		}
		_ = plain.Close()
	}
}

// TestStubClosedServerIsRefused: a closed server is a *UsageError raised before
// any call. Its freed handle would cross as NULL, which the library takes for
// no server and answers, so the binding must refuse it itself.
func TestStubClosedServerIsRefused(t *testing.T) {
	lib := openStub(t, "ok")
	srv, err := lib.NewServer(ServerProfile{Timezone: "UTC"})
	if err != nil {
		t.Fatal(err)
	}
	_ = srv.Close()
	schema, err := lib.CompileTable("CREATE TABLE t (k UInt8) ENGINE = Memory", OnServer(srv))
	var ue *UsageError
	if !errors.As(err, &ue) || !strings.Contains(err.Error(), "server is closed") {
		t.Fatalf("a closed server = %v, %v; want a *UsageError naming the closed server", schema, err)
	}
	if h, err := lib.LiveHandles(); err != nil || h["chs_schema"] != 0 {
		t.Errorf("no schema may exist after a refused compile: %v, %v", h, err)
	}
}

// TestStubServerFromAnotherLibraryIsTheLibrarysToRefuse: the binding does no
// cross-library check; the library's own CHS_INVALID_ARGUMENT reaches the
// caller as a *UsageError carrying that status.
func TestStubServerFromAnotherLibraryIsTheLibrarysToRefuse(t *testing.T) {
	a, b := openStub(t, "ok"), openStub(t, "ok-b")
	srv, err := a.NewServer(ServerProfile{})
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	_, err = b.CompileTable("CREATE TABLE t (k UInt8) ENGINE = Memory", OnServer(srv))
	var ue *UsageError
	if !errors.As(err, &ue) || ue.Status != StatusInvalidArgument {
		t.Errorf("a server from another image = %v, want the library's INVALID_ARGUMENT as a *UsageError", err)
	}
}

// TestStubServerConcurrentCompilesAndClose compiles on one server from many
// goroutines while another closes it: every compile before Close succeeds,
// every one after is the close guard's *UsageError, and nothing crashes (run
// under -race).
func TestStubServerConcurrentCompilesAndClose(t *testing.T) {
	lib := openStub(t, "ok")
	srv, err := lib.NewServer(ServerProfile{Timezone: "UTC"})
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
			for {
				select {
				case <-stop:
					return
				default:
				}
				s, err := lib.CompileTable("CREATE TABLE t (k UInt8) ENGINE = Memory", OnServer(srv))
				if err == nil {
					_ = s.Close()
					continue
				}
				var ue *UsageError
				if !errors.As(err, &ue) {
					badMu.Lock()
					bad = err
					badMu.Unlock()
					return
				}
			}
		}()
	}
	time.Sleep(50 * time.Millisecond)
	_ = srv.Close()
	time.Sleep(20 * time.Millisecond)
	close(stop)
	wg.Wait()
	if bad != nil {
		t.Errorf("a compile failed with something other than the close guard's UsageError: %v", bad)
	}
	if n := liveServers(t, lib); n != 0 {
		t.Errorf("live chs_server after every schema and the server closed = %d", n)
	}
}

// TestStubDescriptionWithoutAServer: the stub's own schema_description (the
// r2 variant's document, generated from the description) carries no server
// member, and decodes with Server and Replicated nil.
func TestStubDescriptionWithoutAServer(t *testing.T) {
	lib := openStub(t, "r2-unknown-members")
	schema, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	defer schema.Close()
	d, err := schema.Describe()
	if err != nil {
		t.Fatal(err)
	}
	if len(d.Columns) != 1 || d.Server != nil || d.Replicated != nil {
		t.Errorf("description without a server = %+v", d)
	}
}

// TestDecodeSchemaDescriptionServer decodes the server and replicated members
// from documents in the shape of spec/abi-v2/abi.json's schema_description.
func TestDecodeSchemaDescriptionServer(t *testing.T) {
	const cols = `"columns":[{"name":"k","type":"UInt8","default_kind":"","default_expression":""}]`

	// Absent: the document is byte for byte the one before servers existed.
	d, err := decodeSchemaDescription([]byte(`{` + cols + `}`))
	if err != nil || d.Server != nil || d.Replicated != nil || len(d.Columns) != 1 {
		t.Fatalf("no server = %+v, %v", d, err)
	}

	// A server described by {}: the image zone, settings {}, macros absent.
	d, err = decodeSchemaDescription([]byte(`{` + cols + `,"server":{"timezone":"UTC","settings":{}}}`))
	if err != nil || d.Server == nil {
		t.Fatalf("server {} = %+v, %v", d, err)
	}
	if d.Server.Timezone != "UTC" || d.Server.Settings == nil || len(d.Server.Settings) != 0 || d.Server.Macros != nil {
		t.Errorf("server {} = %+v: want settings empty (not nil) and macros nil (unknown)", *d.Server)
	}

	// Macros present but empty: the complete set, empty; never read as absent.
	d, err = decodeSchemaDescription([]byte(`{` + cols + `,"server":{"timezone":"Asia/Tokyo","settings":{"max_threads":"8"},"macros":{}}}`))
	if err != nil || d.Server == nil {
		t.Fatalf("macros {} = %+v, %v", d, err)
	}
	if d.Server.Timezone != "Asia/Tokyo" || d.Server.Settings["max_threads"] != "8" || d.Server.Macros == nil || len(d.Server.Macros) != 0 {
		t.Errorf("macros {} = %+v", *d.Server)
	}

	// Macros and a Replicated engine's resolved path, a name in its _b64 form,
	// and members no description names (rule r2), all ignored.
	d, err = decodeSchemaDescription([]byte(`{` + cols + `,
		"server":{"timezone":"UTC","settings":{},"macros":{"shard":"01","replica":"r1"},"x_future":1},
		"replicated":{"zookeeper_path":"/clickhouse/tables/01/db/t","replica_name_b64":"/3Ix","x_future":{"a":[1]}},
		"x_future_top":[1]}`))
	if err != nil || d.Server == nil || d.Replicated == nil {
		t.Fatalf("replicated = %+v, %v", d, err)
	}
	if d.Server.Macros["shard"] != "01" || d.Server.Macros["replica"] != "r1" || len(d.Server.Macros) != 2 {
		t.Errorf("macros = %v", d.Server.Macros)
	}
	if d.Replicated.ZooKeeperPath != "/clickhouse/tables/01/db/t" || d.Replicated.ReplicaName != "\xffr1" {
		t.Errorf("replicated = %+v", *d.Replicated)
	}

	// Refusals: each an *InternalError naming the document.
	for name, doc := range map[string]string{
		"server not an object":           `{` + cols + `,"server":"UTC"}`,
		"a setting that is not a string": `{` + cols + `,"server":{"timezone":"UTC","settings":{"max_threads":8}}}`,
		"macros not an object":           `{` + cols + `,"server":{"timezone":"UTC","settings":{},"macros":[]}}`,
		"timezone not a string":          `{` + cols + `,"server":{"timezone":1,"settings":{}}}`,
		"a path in both forms":           `{` + cols + `,"replicated":{"zookeeper_path":"/a","zookeeper_path_b64":"L2E=","replica_name":"r"}}`,
		"a replica name not base64":      `{` + cols + `,"replicated":{"zookeeper_path":"/a","replica_name_b64":"*"}}`,
	} {
		_, err := decodeSchemaDescription([]byte(doc))
		var ie *InternalError
		if !errors.As(err, &ie) || !strings.Contains(err.Error(), "schema_description") {
			t.Errorf("%s = %v, want an *InternalError naming the document", name, err)
		}
	}
}

// TestDecodeSchemaDescriptionFilterDeclinedSettings decodes the server's
// filter_declined_settings from documents in the shape of
// spec/abi-v2/abi.json's schema_description: [] and absent, entries in the
// profile's own order, a name_b64 entry, a tier the description does not list
// (its unknown(n), rule r3), members no description names (rule r2), and the
// refusals.
func TestDecodeSchemaDescriptionFilterDeclinedSettings(t *testing.T) {
	const cols = `"columns":[{"name":"k","type":"UInt8","default_kind":"","default_expression":""}]`
	server := func(list string) []byte {
		return []byte(`{` + cols + `,"server":{"timezone":"UTC","settings":{"final":"1","aggregate_functions_null_for_empty":"1"},"filter_declined_settings":` + list + `}}`)
	}

	// [] is present and empty; absent (a document of no build at this
	// fingerprint) is nil. Neither is a failure.
	d, err := decodeSchemaDescription(server(`[]`))
	if err != nil || d.Server == nil || d.Server.FilterDeclinedSettings == nil || len(d.Server.FilterDeclinedSettings) != 0 {
		t.Fatalf("[] = %+v, %v; want a non-nil empty list", d.Server, err)
	}
	d, err = decodeSchemaDescription([]byte(`{` + cols + `,"server":{"timezone":"UTC","settings":{}}}`))
	if err != nil || d.Server == nil || d.Server.FilterDeclinedSettings != nil {
		t.Fatalf("absent = %+v, %v; want nil", d.Server, err)
	}

	// The profile's own order, never sorted; a name_b64 entry decodes to its
	// bytes; an unlisted tier is kept as it came; unknown members are ignored.
	d, err = decodeSchemaDescription(server(`[{"name":"final","tier":"result-content","x_future":1},` +
		`{"name":"aggregate_functions_null_for_empty","tier":"predicate"},` +
		`{"name_b64":"eP95","tier":"predicate-unflipped"},` +
		`{"name":"x_future_setting","tier":"x_future_tier","x_future_obj":{"a":[1]}}]`))
	if err != nil || d.Server == nil {
		t.Fatalf("entries = %+v, %v", d, err)
	}
	want := []DeclinedSetting{
		{Name: "final", Tier: TierResultContent},
		{Name: "aggregate_functions_null_for_empty", Tier: TierPredicate},
		{Name: "x\xffy", Tier: TierPredicateUnflipped},
		{Name: "x_future_setting", Tier: "x_future_tier"},
	}
	if !reflect.DeepEqual(d.Server.FilterDeclinedSettings, want) {
		t.Errorf("entries = %#v, want %#v", d.Server.FilterDeclinedSettings, want)
	}
	for i, e := range d.Server.FilterDeclinedSettings {
		if e.Tier.Known() != (i < 3) {
			t.Errorf("entry %d: tier %q Known() = %v", i, e.Tier, e.Tier.Known())
		}
	}

	// Refusals: each an *InternalError naming the document.
	for name, list := range map[string]string{
		"not an array":            `{"name":"final","tier":"predicate"}`,
		"an entry not an object":  `["final"]`,
		"a name in both forms":    `[{"name":"final","name_b64":"ZmluYWw=","tier":"predicate"}]`,
		"no name":                 `[{"tier":"predicate"}]`,
		"a name not base64":       `[{"name_b64":"*","tier":"predicate"}]`,
		"a tier that is a number": `[{"name":"final","tier":1}]`,
	} {
		_, err := decodeSchemaDescription(server(list))
		var ie *InternalError
		if !errors.As(err, &ie) || !strings.Contains(err.Error(), "schema_description") {
			t.Errorf("%s = %v, want an *InternalError naming the document", name, err)
		}
	}
}
