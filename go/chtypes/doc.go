// Package chtypes is ClickHouse's own C++ type machinery, vendored, wrapped in
// a C ABI and bound to Go: the answer to "if this row were inserted into this
// table on this ClickHouse version, what would happen?" comes from
// ClickHouse's own code, never from a model of it.
//
// This module, github.com/wave-rf/chtypes/go/v2, is the ABI v2 binding: it
// speaks the locked ABI v2 description (docs/reference/abi-v2.md, rules r1 to
// r9), refuses a library with any other fingerprint, and fetches from the
// production channel, https://registry.wavehouse.dev/chtypes/v2 under the
// release key (docs/guides/fetch-v1.md, "Generation 2 after the lock"). A value
// a vocabulary does not list reads as that type's unknown(n): Known reports
// false for it, and it never fails a document.
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
// AsCallError reads the six fields every one of the four carries. A
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
//
// # Cancellation
//
// The calls that read a body or compile a table each have a context variant,
// with ctx first: Schema.RowContext, Schema.RowsContext, Schema.InsertContext,
// Schema.ParseBlockContext, Filter.RowsContext and Library.CompileTableContext.
// When ctx is done before the call returns its answer, the call stops at its
// next check point and returns a *CanceledError (CHS_CANCELED, reason
// call-canceled) whose Cause is context.Cause(ctx), so
// errors.Is(err, context.Canceled) and errors.Is(err, context.DeadlineExceeded)
// hold; it returns no result, no block and no schema. A ctx already done
// cancels the call at entry, before any byte of the body is read. A call that
// finished its answer before it saw the cancellation returns that answer.
// One ctx may cover any number of concurrent calls.
//
// With a ctx that can never be done (context.Background, context.TODO) a
// context variant is exactly the plain call: no token, no goroutine and no
// extra ABI call. Filter.Eval has no context variant: evaluating a parsed
// block is not cancelable. How soon a cancellation is seen is the library's
// (spec/abi-v2/docs.md, chs_cancel): one long ClickHouse action runs to its
// end first.
//
//	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
//	defer cancel()
//	res, err := schema.RowsContext(ctx, chtypes.JSONEachRow, body)
//	var ce *chtypes.CanceledError
//	if errors.As(err, &ce) { /* ce.Cause is context.DeadlineExceeded */ }
package chtypes
