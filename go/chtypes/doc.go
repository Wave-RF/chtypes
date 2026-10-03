// Package chtypes is ClickHouse's own C++ type machinery, vendored, wrapped in
// a C ABI and bound to Go: the answer to "if this row were inserted into this
// table on this ClickHouse version, what would happen?" comes from
// ClickHouse's own code, never from a model of it.
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
// Schema, Filter or Block has a close guard, so Close waits for the calls
// already inside that object, and a later call is a *UsageError.
package chtypes
