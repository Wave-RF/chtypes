// Package chtypes is ClickHouse's own C++ type machinery, vendored, wrapped in
// a C ABI and bound to Go: the answer to "if this row were inserted into this
// table on this ClickHouse version, what would happen?" comes from
// ClickHouse's own code, never from a model of it.
//
// 2.0.0-dev: UNSTABLE, staging only, not for production. This module,
// github.com/wave-rf/chtypes/go/v2, is the ABI v2 development binding (public
// issue #511): it speaks the unstable ABI v2 description, refuses a library
// with any other fingerprint, and fetches only from the staging dev channel
// under the staging key, with no override and no lock (docs/reference/abi-v2.md,
// rules r1 to r6). A value a vocabulary does not list reads as that type's
// unknown(n): Known reports false for it, and it never fails a document.
//
// The surface is docs/reference/bindings-v1.md. A binding is a thin
// passthrough: every public call makes exactly one ABI call, and no scalar,
// comparison, coercion, timestamp, zone, quoting or classification rule lives
// here. The package is dlopen-only by default (OpenLinked, behind
// -tags chtypes_linked, is the statically linked path).
//
// # Opening a library
//
// Call Setup once, before anything else, when the process needs a time zone
// or default settings; then build a Registry and ask it for a ClickHouse
// version:
//
//	reg, err := chtypes.NewRegistry(chtypes.WithAutoFetch(true))
//	lib, err := reg.For("26.8")
//	schema, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = MergeTree ORDER BY x")
//	defer schema.Close()
//	res, err := schema.Row(chtypes.JSONEachRow, []byte(`{"x": 256}`))
//
// # A server
//
// A table can be compiled on one ClickHouse server, which a profile
// describes: its timezone, the settings its profile applies to every query,
// and its macros. The schema then binds the server's zone, as a table on that
// server does:
//
//	srv, err := lib.NewServer(chtypes.ServerProfile{Timezone: "Asia/Tokyo"})
//	defer srv.Close()
//	schema, err := lib.CompileTable(
//		"CREATE TABLE t (k UInt8, tz String DEFAULT timezone()) ENGINE = MergeTree ORDER BY k",
//		chtypes.OnServer(srv))
//	res, err := schema.Row(chtypes.JSONEachRow, []byte(`{"k":1}`)) // tz is Asia/Tokyo
//
// Every profile field is optional and passed through as given; the library
// judges the zone, the settings and the macros. A nil Macros means the
// server's macros are unknown, and a non-nil one, even empty, is its complete
// set. A Server is immutable, so it is safe for concurrent use, and a schema
// holds its own reference to its server inside the library, so the two close
// in any order. Schema.Describe reports the server a schema was compiled on.
//
// # Errors
//
// A call that ClickHouse refuses is a *SchemaError; one this build declines to
// answer is a *UnsupportedError, a PEER of the first that errors.As never
// confuses with it. *UsageError is misuse (a closed object, a refused
// version spelling, a conflicting setup) and *InternalError a library bug.
// AsCallError reads the five fields every one of the four carries. A
// loader or fetch failure is an *ArtifactError whose Code is the shared
// vocabulary, with a sentinel per code for errors.Is.
//
// # Bytes
//
// A Go string is a byte string: every name, statement, expression, type,
// quoted spelling, message and rendered value is built from copied bytes and
// may hold NUL or invalid UTF-8.
//
// # Threads
//
// Everything here is safe for concurrent use. No call takes a lock; a
// Server, Schema, Filter or Block has a close guard, so Close waits for the
// calls already inside that object, and a later call is a *UsageError.
package chtypes
