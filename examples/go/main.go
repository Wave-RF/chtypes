// Command playground is the Go tour of chtypes, on the v1 public API
// (docs/reference/bindings-v1.md).
//
// # WHAT THIS IS
//
// chtypes answers one question: "if this row were inserted into this table on
// this ClickHouse version, what would happen?" without a server. The answers
// come from ClickHouse's own C++, vendored per release into a shared library
// the binding loads and speaks to over a C ABI, which is why they are exact
// rather than approximately right.
//
// This file is a tutorial you RUN. Seventeen numbered sections walk the whole
// public API of the Go SDK, from opening a library to tearing down, each with
// a comment saying what it demonstrates, why an ingest pipeline cares, and
// what to look at in the output. The same seventeen sections exist in the
// Python, TypeScript and Rust tours, so you can diff two tours and see only
// the language idioms differ.
//
// Everything runs offline against an INSTALLED artifact: fetch one first,
//
//	go run ../../go/cmd/chtypes fetch 26.8
//
// then run the tour (no Docker, no ClickHouse server; even the discovery-kit
// section runs against canned bytes):
//
//	go run .                       # newest installed line
//	CHTYPES_VERSION=26.8 go run .  # pick a line
//	../chplay.sh go                # same, with prerequisite checks
//
// Nothing here is a test: the real suites live under go/ and tests/. Every
// number printed below is produced by the run, never written down by hand.
package main

import (
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"runtime"
	"sort"
	"strconv"
	"strings"

	"github.com/wave-rf/chtypes/go/chtypes"
)

// ---------------------------------------------------------------- the fixture
//
// These constants are IDENTICAL in all four tours: the point of this
// directory is that the four outputs can be diffed.

// The tenant's table for the DEFAULT and result sections: one of everything
// the tour needs, a volatile DEFAULT (now64), a literal DEFAULT, an Enum (how
// a table gets poisoned), and a MATERIALIZED column (never in SELECT *).
const demoDDL = `CREATE TABLE t (
ts DateTime64(3) DEFAULT now64(3),
device_id UInt32,
seq UInt8 DEFAULT 0,
payload String,
grade Enum8('a' = 1, 'b' = 2),
payload_len UInt32 MATERIALIZED length(payload)
) ENGINE = MergeTree ORDER BY device_id`

// A small three-column table for the outcome and format sections. Positional
// formats (CSV, TSV, Values, RowBinary...) are far easier to read against a
// small schema, and both DEFAULTs give the empty-field rules something to do.
const formatDDL = `CREATE TABLE t (device_id UInt32, seq UInt8 DEFAULT 7, label String DEFAULT 'unknown') ENGINE = MergeTree ORDER BY device_id`

// tableOf wraps a column list in the one CREATE TABLE statement every compile
// takes (there is no column-list compile in v1).
func tableOf(columns string) string {
	return "CREATE TABLE t (" + columns + ") ENGINE = MergeTree ORDER BY tuple()"
}

// Hand-built binary payloads (hex), shared by all four tours. Each is
// explained where it is fed.
const (
	rowBinaryOK = "0100000007026f6b"     // UInt32 LE 1, UInt8 7, varint-len "ok"
	rbwdMarker  = "00020000000100026869" // device_id=2 by value, seq by marker, label="hi"
	rbwdPoison  = "000100000001"         // id=1 by value, e by marker -> raw 0, NO name
	rbwdValue   = "00010000000002"       // id=1 by value, e by value 2 ("green")
	// RowBinaryWithNamesAndTypesAndDefaults: LEB128 column count, names,
	// types, then RBWD-style marker+value rows. Built by hand for formatDDL:
	// 3 cols, device_id=1 by value, seq by marker (DEFAULT 7), label="ok".
	rbwntdRow = "03" + "096465766963655f6964" + "03736571" + "056c6162656c" +
		"0655496e743332" + "0555496e7438" + "06537472696e67" +
		"00" + "01000000" + "01" + "00" + "026f6b"
	// Native blocks captured from a live 25.8 (SELECT ... FORMAT Native).
	nativeOK   = "0301096465766963655f69640655496e74333201000000037365710555496e743807056c6162656c06537472696e67026f6b"
	nativeCast = "0301096465766963655f69640655496e743332020000000373657106537472696e6703323030056c6162656c06537472696e670463617374"
	// Buffers: uint64le n_columns, n_rows, then per column a byte size and the
	// raw column. 4 bytes ff ff ff ff, declared 4 wide, then (wrongly) 8 wide.
	buffersOK   = "010000000000000001000000000000000400000000000000ffffffff"
	buffersWide = "010000000000000001000000000000000800000000000000ffffffff"
)

// Section 11's CANNED server answer: the JSONEachRow rows of the library's own
// discovery query (system.columns), shaped like a stock HTTP server's output
// (quoted UInt64s and all). Swap in your own HTTP client's bytes and nothing
// else changes. The server's version, and the settings it changed from stock,
// are the CALLER's to supply in v1 (the library has no call that asks a
// server), so they are plain values here.
const (
	cannedServerVersion = "25.8.28.1"
	cannedColumnsResult = `{"name":"ts","type":"DateTime64(3)","default_kind":"DEFAULT","default_expression":"now64(3)"}` + "\n" +
		`{"name":"device_id","type":"UInt32","default_kind":"","default_expression":""}` + "\n" +
		`{"name":"reading c","type":"Float64","default_kind":"","default_expression":""}` + "\n" +
		`{"name":"note","type":"String","default_kind":"DEFAULT","default_expression":"'unset'"}` + "\n"
)

// setupDefaults is the process-wide default settings layer (section 9). It is
// fixed once, before any library opens, and lives under every call's own
// settings: a stock "basic" datetime parse, so section 9 can show each layer
// overriding the one below it.
var setupDefaults = map[string]string{"date_time_input_format": "basic"}

func main() {
	// The process setup comes first and only once (bindings-v1.md section 6):
	// the image zone and the default settings are fixed before any library
	// loads, and a different setup later is a *UsageError.
	must(chtypes.Setup(chtypes.SetupOptions{Defaults: setupDefaults}))

	reg, lib := section1()
	section2(lib)
	section3(lib)
	section4(lib)
	section5(lib)
	section6(lib)
	section7(lib, reg)
	section8(lib)
	section9(lib)
	section10(lib)
	section11(reg, lib)
	section12(reg)
	section13()
	section14()
	section15(lib)
	section16(lib)
	section17(lib)

	blank()
	line("Done. Every value above was measured by this run.")
	line("The optional ONLINE demo (a real server, end to end) is ingest-demo/.")
}

// ---------------------------------------------------------------------------
// SECTION 1 — Open a library and read its build info
//
// WHAT: construct a registry, see what the fetch layer holds, open one
// version, and read the library's own account of itself.
// WHY: an ingest gateway serves tenants on different ClickHouse versions at
// once; the registry is how one process answers for all of them, exactly.
// LOOK FOR: the library NAMING ITSELF (version, line, fingerprint), and the
// refusal for a version that is not installed, never a nearest-version
// fallback.
// C API: chs_build_info (read once by the loader), after the load steps of
// docs/reference/abi-v1.md.
// ---------------------------------------------------------------------------
func section1() (*chtypes.Registry, *chtypes.Library) {
	section(1, "Open a library and read its build info")

	reg, err := chtypes.NewRegistry()
	if err != nil {
		fatal("construct a registry: %v", err)
	}
	installed, err := reg.Installed()
	if err != nil {
		fatal("list the installed builds: %v", err)
	}
	lines := installedLines(installed)
	kv("lines installed", strings.Join(lines, "  "))
	note("one dlopen per library, never unloaded: every line lives in THIS")
	note("process at once. Fetch more with `chtypes fetch <spelling>`.")
	if len(lines) == 0 {
		fatal("nothing is installed for %s.\n\nFetch an artifact first: `go run ../../go/cmd/chtypes fetch 26.8`.", hostPlatform())
	}
	if len(lines) == 1 {
		note("only one line is installed; the tour still runs, and section 12's")
		note("cross-version sweeps will degrade gracefully.")
	}

	// A request is a spelling: a two-part line ("26.8"), three parts, or an
	// exact four-part version. The fetch layer decides which installed build
	// a floating request means; the binding never orders versions.
	want := os.Getenv("CHTYPES_VERSION")
	if want == "" {
		want = lines[len(lines)-1]
		kv("version requested", want+"  (default: newest installed; set $CHTYPES_VERSION to change)")
	} else {
		kv("version requested", want+"  (from $CHTYPES_VERSION)")
	}
	lib, err := reg.For(want)
	if err != nil {
		fatal("%v", err)
	}
	kv("library reports", lib.Version+"  (minor line "+lib.Minor+")")
	note("the library names ITSELF from chs_build_info; nothing is ever")
	note("inferred from a directory or file name")

	// Identity is the ABI generation plus the fingerprint, checked by the
	// loader before any call is made; a mismatch is *ArtifactError, never a
	// crash mid-call.
	info := lib.BuildInfo()
	kv("ABI generation", strconv.Itoa(info.ABI))
	kv("ABI fingerprint", info.ABIFingerprint)
	kv("build", info.Build+"  (core "+shortHash(info.CoreCommit)+", ClickHouse "+shortHash(info.ClickHouseCommit)+")")
	if res := lib.Resolved(); res != nil {
		kv("opened from", res.Source+"  (signed by key "+res.SignedBy+")")
	}

	// Graceful refusal: answering 26.7 semantics out of a 25.8 library would
	// be a lie, so an uninstalled version errors, NAMING how to get it.
	blank()
	_, err = reg.For("99.9")
	kv("asking for 99.9", errStr(err))
	var ae *chtypes.ArtifactError
	if errors.As(err, &ae) {
		kv("  error code", string(ae.Code))
	}
	note("no nearest-neighbor fallback, ever: a wrong-version answer is a")
	note("wrong answer with a green checkmark on it")

	return reg, lib
}

// ---------------------------------------------------------------------------
// SECTION 2 — Ask a build about itself
//
// WHAT: type validation and canonicalization, quoting, and the build's own
// error-code table and capabilities.
// WHY: canonicalization is how you compare a tenant's declared type against
// what the server will actually store, and it is NOT a spelling normalizer,
// it is the server's own parse.
// LOOK FOR: Variant members being SORTED, BIGINT becoming Int64, the error
// for an unknown family carrying ClickHouse's own code 50, and quoting that
// is the library's own backQuote.
// C API: chs_type_validate, chs_back_quote(_if_needed), chs_quote_string,
// chs_error_codes.
// ---------------------------------------------------------------------------
func section2(lib *chtypes.Library) {
	section(2, "Ask a build about itself")

	kv("ValidateType", "input -> this build's canonical spelling")
	for _, t := range []string{"Decimal(18,4)", "Variant(UInt8, String)", "BIGINT", "LowCardinality( String )"} {
		canon, err := lib.ValidateType(t)
		if err != nil {
			kv("  "+t, errStr(err))
			continue
		}
		kv("  "+t, "-> "+canon)
	}
	note("Variant members are SORTED; the space after each comma is the")
	note("library's own spelling: compare canonical strings verbatim, never")
	note("re-normalize whitespace")
	blank()

	// An unknown family is a typed error carrying ClickHouse's OWN code and
	// message, not a string you have to pattern-match.
	_, err := lib.ValidateType("NotAType")
	var se *chtypes.SchemaError
	if errors.As(err, &se) {
		kv("ValidateType(NotAType)", fmt.Sprintf("*SchemaError code=%d %s  %s", se.ChCode, se.ChName, truncate(se.Message, 48)))
		note("code 50 = UNKNOWN_TYPE: the server's own code, from the server's")
		note("own registry. Section 10 is the full error taxonomy.")
	}
	blank()

	// Quoting is ClickHouse's own: bytes in, bytes out, never a copy of the
	// rule in the binding.
	for _, name := range []string{"plain", "reading c", "a`b"} {
		always, err1 := lib.QuoteIdentifier(name)
		needed, err2 := lib.QuoteIdentifierIfNeeded(name)
		if err1 != nil || err2 != nil {
			kv("  quote "+name, errStr(errors.Join(err1, err2)))
			continue
		}
		kv("  quote "+name, fmt.Sprintf("always %s   if needed %s", always, needed))
	}
	if lit, err := lib.QuoteLiteral("it's"); err == nil {
		kv("  QuoteLiteral(it's)", lit)
	}
	blank()

	// The build's own error-code table: this ClickHouse line's names for its
	// codes. An unknown code is absent, never synthesized.
	table, err := lib.ErrorCodes()
	if err != nil {
		kv("ErrorCodes", errStr(err))
	} else {
		name, _ := table.Name(50)
		kv("ErrorCodes", fmt.Sprintf("%d codes; 50 is %s", len(table.All()), name))
	}
	caps := lib.BuildInfo().Capabilities
	kv("capabilities", fmt.Sprintf("%d input formats, %d export formats, features %v", len(caps.InputFormats), len(caps.ExportFormats), caps.Features))
	note("what a build supports is READ from build_info, never probed by calling")
	note("(v1 deletes the v0 introspection trio; see bindings-v1.md section 7)")
}

// ---------------------------------------------------------------------------
// SECTION 3 — Compile a schema and read it back
//
// WHAT: compile ONE CREATE TABLE statement and walk the compiled columns:
// canonical types, DEFAULT kinds and expressions.
// WHY: the compiled handle IS the table, as this ClickHouse version would
// create it. Two rewrites below are things no type-string comparison could
// ever catch: the compile is schema-aware.
// LOOK FOR: DEFAULT NULL turning Int64 into Nullable(Int64), and an ALIAS
// column whose type is INFERRED from its expression.
// C API: chs_schema_create, chs_schema_describe, chs_schema_free (via Close).
// ---------------------------------------------------------------------------
func section3(lib *chtypes.Library) {
	section(3, "Compile a schema and read it back")

	kv("the statement", "")
	for _, l := range strings.Split(demoDDL, "\n") {
		raw("      " + l)
	}
	s, err := lib.CompileTable(demoDDL)
	if err != nil {
		fatal("%v", err)
	}
	desc, err := s.Describe()
	must(err)
	blank()
	kv("compiled columns", "name  type  (default kind + expression)")
	for _, c := range desc.Columns {
		extra := ""
		if c.DefaultKind != chtypes.KindNone {
			extra = "  " + string(c.DefaultKind) + " " + c.DefaultExpr
		}
		kv("  "+c.Name, c.Type+extra)
	}
	note("payload_len is MATERIALIZED: compiled, introspectable, but never")
	note("read from input; watch it come back separately in section 6")
	must(s.Close()) // chs_schema_free; a closed handle is a *UsageError
	blank()

	// Rewrite 1: a DEFAULT can change the declared TYPE. This is why
	// ValidateType alone is not enough and a schema-aware compile exists.
	s2, err := lib.CompileTable(tableOf("x Int64 DEFAULT NULL"))
	must(err)
	d2, _ := s2.Describe()
	kv("x Int64 DEFAULT NULL", "compiles as "+d2.Columns[0].Type+" DEFAULT "+d2.Columns[0].DefaultExpr)
	note("the DEFAULT rewrote the type to Nullable: the server does this at")
	note("CREATE, so chtypes must too or every later verdict drifts")
	s2.Close()

	// Rewrite 2: an ALIAS column's type is inferred from its expression.
	s3, err := lib.CompileTable(tableOf("a UInt8, al ALIAS a + 1"))
	must(err)
	d3, _ := s3.Describe()
	kv("a UInt8, al ALIAS a + 1", "al compiles as "+d3.Columns[1].Type)
	note("UInt8 + 1 widens to UInt16, ClickHouse's own inference")
	s3.Close()
	blank()

	// A failed compile is the same typed error as section 2's.
	_, err = lib.CompileTable(tableOf("x NotAType"))
	kv("x NotAType", classify(err))
	note(truncate(errStr(err), 90))
}

// ---------------------------------------------------------------------------
// SECTION 4 — Compile under a settings profile
//
// WHAT: the same compile with a profile of settings: the settings a real
// server would have had at CREATE TABLE.
// WHY: some settings change the SHAPE of a table (flatten_nested), some gate
// which TYPES may exist (allow_suspicious_low_cardinality_types). A gateway
// discovers a deployment's settings once (section 11) and declares them here.
// LOOK FOR: one statement compiling to two different column lists; a type
// gate failing with the server's own code; a typo'd setting name failing with
// the server's own 115 INCLUDING its did-you-mean hint.
// C API: chs_schema_create (the settings argument).
// ---------------------------------------------------------------------------
func section4(lib *chtypes.Library) {
	section(4, "Compile under a settings profile")

	// (a) A compile-SHAPE setting: the same statement, two storage shapes.
	kv("(a) flatten_nested", "id UInt32, n Nested(a UInt8, b String)")
	for _, v := range []string{"1", "0"} {
		s, err := lib.CompileTable(tableOf("id UInt32, n Nested(a UInt8, b String)"),
			chtypes.WithSettings(map[string]string{"flatten_nested": v}))
		if err != nil {
			kv("  ="+v, errStr(err))
			continue
		}
		d, _ := s.Describe()
		var names []string
		for _, c := range d.Columns {
			names = append(names, c.Name+" "+c.Type)
		}
		kv("  ="+v, strings.Join(names, " | "))
		s.Close()
	}
	note("under 1 (the stock default) the Nested column is stored FLATTENED as")
	note("two Arrays; under 0 it is one Array(Tuple) column. Every downstream")
	note("answer (names, arity, the RowBinary wire) follows the compiled shape.")
	blank()

	// (b) A TYPE GATE, declared: checked ONCE at compile with the server's own
	// code, exactly where a real server checks it (at CREATE).
	kv("(b) a type gate", "lc LowCardinality(UInt8), allow_suspicious_low_cardinality_types")
	for _, v := range []string{"0", "1"} {
		s, err := lib.CompileTable(tableOf("lc LowCardinality(UInt8)"),
			chtypes.WithSettings(map[string]string{"allow_suspicious_low_cardinality_types": v}))
		if err != nil {
			kv("  ="+v, "REFUSED, "+classify(err))
			note(truncate(errStr(err), 92))
		} else {
			d, _ := s.Describe()
			kv("  ="+v, "compiled: "+d.Columns[0].Type)
			s.Close()
		}
	}
	blank()

	// (c) A typo in the profile is caught at DECLARE time. The did-you-mean
	// hint is the SERVER'S: chtypes passes it through and invents nothing.
	_, err := lib.CompileTable(tableOf("a UInt8"), chtypes.WithSettings(map[string]string{"flatten_nestedd": "1"}))
	var se *chtypes.SchemaError
	if errors.As(err, &se) {
		kv("(c) unknown setting name", fmt.Sprintf("flatten_nestedd -> code %d %s", se.ChCode, se.ChName))
		note(truncate(se.Message, 96))
	}
	blank()

	// (d) The statement's own engine and settings are part of the one
	// statement; a schema is immutable once compiled (v0's SetEngine,
	// SetTTL and compile modes are gone, bindings-v1.md section 7).
	kv("(d) one statement", "engine, TTL and partition key are in the CREATE TABLE text")
	note("section 8 shows the engine layer; there is no setter after the compile")
}

// ---------------------------------------------------------------------------
// SECTION 5 — Accept, reject, decline (and poison)
//
// WHAT: the verdicts every row lands on, plus the fourth, poisoned, which
// looks like an accept and bites at read time, and the fifth, skipped.
// WHY: this is the contract of the whole product. An ingest gateway routes on
// exactly this: accepted -> insert and publish the STORED values; rejected ->
// 400 the producer with the server's own message; unsupported -> chtypes
// refuses to guess, so fall back to the real server and NEVER convert the
// decline into an accept or a reject yourself.
// LOOK FOR: the accept carrying a visible coercion (input 256, stored 0);
// the reject carrying ClickHouse's own error text; the decline carrying no
// ClickHouse code at all.
// C API: chs_preview_row, chs_preview_batch.
// ---------------------------------------------------------------------------
func section5(lib *chtypes.Library) {
	section(5, "Accept, reject, decline (and poison)")
	kv("schema", formatDDL)
	blank()

	s, err := lib.CompileTable(formatDDL)
	if err != nil {
		fatal("%v", err)
	}
	defer s.Close()

	// ACCEPT, with the coercion made visible. ClickHouse's readIntText wraps
	// integers mod 2^N and reports SUCCESS; the library reports the Transform
	// so a gateway can warn the tenant BEFORE the row ships to subscribers.
	kv("(a) ACCEPT", `{"device_id":1,"seq":256,"label":"ok"}`)
	feed(s, "JSONEachRow", chtypes.JSONEachRow, []byte(`{"device_id":1,"seq":256,"label":"ok"}`))
	note("accepted, but look at seq: input 256, stored 0. The ~ line is the")
	note("Transform (reason overflow_wrap, LOSSY, read from the document).")
	note("Publish the STORED value; publishing the payload value is how")
	note("previews and tables diverge.")
	blank()

	// REJECT: the server's own refusal, code and message verbatim from the
	// vendored ClickHouse code. Nothing to retry; tell the producer.
	kv("(b) REJECT", `{"device_id":"abc"}`)
	feed(s, "JSONEachRow", chtypes.JSONEachRow, []byte(`{"device_id":"abc"}`))
	note("the code and the message are ClickHouse's OWN: chtypes never")
	note("hand-writes an error, so your 400 body matches what a real INSERT")
	note("would have said")
	blank()

	// DECLINE: chtypes refuses to guess. Values falls back to the SQL
	// expression parser for non-literals; evaluating tenant SQL locally is a
	// guess this library will not make.
	kv("(c) DECLINE", "(2,7,concat('a','b'))  as Values")
	feed(s, "Values", chtypes.Values, []byte(`(2,7,concat('a','b'))`))
	note("unsupported means 'a real server MIGHT WELL accept this; I will not")
	note("guess'. Do not 400 the producer (that manufactures an over-reject),")
	note("do not publish (that manufactures an over-accept): send it to the")
	note("real server unpreviewed and let it decide.")
	blank()

	// POISON: the INSERT genuinely succeeds and every later SELECT throws:
	// RowBinaryWithDefaults' marker byte fills an Enum with the raw zero, and
	// Enum8('red'=1,'green'=2) has NO name for 0.
	kv("(d) POISON", "an accept that bites at read time")
	sp, err := lib.CompileTable(tableOf("id UInt32, e Enum8('red' = 1, 'green' = 2)"))
	if err == nil {
		kv("  schema", "id UInt32, e Enum8('red' = 1, 'green' = 2)")
		kv("  payload (RBWD)", rbwdPoison+"   (id=1 by value, e by marker byte)")
		feed(sp, "RBWD marker", chtypes.RowBinaryWithDefaults, unhex(rbwdPoison))
		note("accepted_poisoned: the marker fills with the COLUMN-level raw zero,")
		note("and raw 0 has no Enum name. The insert returns success; every later")
		note("SELECT fails. Reported as an ACCEPT variant, never a rejection,")
		note("because the insert really does succeed.")
		kv("  control payload", rbwdValue+"   (e supplied by value = 2)")
		feed(sp, "RBWD value", chtypes.RowBinaryWithDefaults, unhex(rbwdValue))
		sp.Close()
	}
	blank()

	// SKIP: the only per-row-only verdict. A batch under
	// input_format_allow_errors_* drops a bad row and continues, with the
	// server's own machinery, and every skip keeps its place in Rows with the
	// error the reader caught before resyncing.
	kv("(e) SKIP", "a bad middle row under input_format_allow_errors_num=10")
	batch := []byte(`{"device_id":1,"seq":1,"label":"a"}` + "\n" +
		`{"device_id":"oops"}` + "\n" +
		`{"device_id":3,"seq":3,"label":"c"}` + "\n")
	b, err := s.Rows(chtypes.JSONEachRow, batch,
		chtypes.WithSettings(map[string]string{"input_format_allow_errors_num": "10"}))
	if err != nil {
		fatal("%v", err)
	}
	kv("  batch", fmt.Sprintf("%s  rows_read=%d rows_skipped=%d", b.Outcome, b.RowsRead, b.RowsSkipped))
	for i, r := range b.Rows {
		l := string(r.Outcome)
		if r.Outcome == chtypes.Skipped {
			l += fmt.Sprintf("  code=%d %s", r.ErrCode, truncate(r.ErrMsg, 48))
		}
		kv(fmt.Sprintf("  row %d", i), l)
	}
	note("one Rows call answers per input record, IN ORDER: accepted (with")
	note("the coerced values) or skipped (with the error that caused it).")
	note("A skipped row is never stored: route on the row Outcome; forward")
	note("only survivors, and never send allow_errors to the real INSERT.")
}

// ---------------------------------------------------------------------------
// SECTION 6 — DEFAULT evaluation: where every value comes from
//
// WHAT: one row through the demo table, then reading back WHERE each stored
// value came from (Value.Source), which entries the library computed, and
// which it generated.
// WHY: an INSERT is mostly values the row did NOT supply. A gateway that
// cannot answer "what will the table hold for this column?" cannot preview an
// insert.
// LOOK FOR: several different Source values in one row; payload_len under
// Computed (never in Values); "bogus" under UnknownFields; and, when the build
// lists the default_generators feature, a generated DEFAULT you must insert
// from the library's output.
// C API: chs_preview_row.
// ---------------------------------------------------------------------------
func section6(lib *chtypes.Library) {
	section(6, "DEFAULT evaluation: where every value comes from")

	s, err := lib.CompileTable(demoDDL)
	if err != nil {
		fatal("%v", err)
	}
	defer s.Close()

	row := []byte(`{"device_id":42,"seq":256,"payload":"hello","grade":"a","bogus":1}`)
	kv("row fed", string(row))
	note("ts is OMITTED on purpose; bogus matches no column")
	r, err := s.Row(chtypes.JSONEachRow, row)
	if err != nil {
		fatal("%v", err)
	}
	blank()

	kv("Outcome / ErrCode", fmt.Sprintf("%v / %d", r.Outcome, r.ErrCode))
	kv("Values[]  (the stored row)", "column = stored text  (Source)")
	for _, v := range r.Values {
		kv("  "+v.Column, fmt.Sprintf("%-28s (%s)", textOr(v), v.Source))
	}
	note("Source values: input (the row supplied it), default (a DEFAULT")
	note("evaluated through ClickHouse's own CAST path), default_substituted")
	note("(a VOLATILE default the library resolved from its own clock), absent")
	note("(no DEFAULT: the type's own zero). Text is ClickHouse's OWN rendering,")
	note("never re-serialized here.")

	blank()
	kv("Columns[]", "every entry the library reports, stored or not")
	for _, c := range r.Columns {
		kv("  "+c.Column, fmt.Sprintf("%-10s is_stored=%v", c.Source, c.IsStored))
	}
	note("is_stored is the description's own fact for the source: an EPHEMERAL")
	note("input or an unresolved DEFAULT is reported but stores nothing")

	blank()
	kv("Computed[]", "MATERIALIZED values: durable, but never in SELECT *")
	for _, c := range r.Computed {
		kv("  "+c.Column, c.Kind+"  =  "+c.Text)
	}
	note("reported separately from Values so the preview matches what a")
	note("subscriber reading the table will actually see")

	blank()
	kv("Transformed[]", "every silent change, with a machine-readable reason")
	for _, t := range r.Transformed {
		kv("  "+string(t.Reason), fmt.Sprintf("%s: %s -> %s   lossy=%v", t.Column, or(t.Input, "(absent)"), t.Stored, t.Lossy))
	}
	note("lossy is READ from the document: the library decides, and the")
	note("binding keeps no list of reasons of its own")

	blank()
	kv("UnknownFields[]", fmt.Sprintf("%q", r.UnknownFields))
	kv("UnsupportedSettings[]", fmt.Sprintf("%q", r.UnsupportedSettings))
	note("unknown fields are reported, not judged: whether to 400 on them is")
	note("gateway policy")
	blank()

	// A DEFAULT can read OTHER columns of the same row.
	s2, err := lib.CompileTable(tableOf("a UInt8, d UInt8 DEFAULT a + 1"))
	if err == nil {
		kv("row-dependent DEFAULT", "a UInt8, d UInt8 DEFAULT a + 1   fed CSV `7,`")
		feed(s2, "CSV", chtypes.CSV, []byte("7,"))
		note("the bare empty CSV field takes the DEFAULT, and the DEFAULT reads")
		note("a=7 from the same row: d stores 8, exactly as the server computes it")
		s2.Close()
	}
	blank()

	// A DEFAULT that calls an admitted random or ID generator: the library
	// draws the value itself, so the CALLER must insert the library's output.
	if hasFeature(lib, "default_generators") {
		s3, err := lib.CompileTable(tableOf("id UInt32, token UUID DEFAULT generateUUIDv4()"))
		must(err)
		defer s3.Close()
		kv("generated DEFAULT", "token UUID DEFAULT generateUUIDv4()   fed {\"id\":1}")
		gr, err := s3.Row(chtypes.JSONEachRow, []byte(`{"id":1}`))
		must(err)
		for _, v := range gr.Values {
			kv("  "+v.Column, fmt.Sprintf("%-38s (%s)", textOr(v), v.Source))
		}
		note("default_generated: the library drew this UUID. A real server would")
		note("draw a DIFFERENT one for the same row, so insert the library's")
		note("EXPORT (section 15), never your original input.")
	} else {
		kv("generated DEFAULT", "this build does not list default_generators; skipped")
	}
}

// ---------------------------------------------------------------------------
// SECTION 7 — One schema, every format
//
// WHAT: the same three-column schema fed in all twelve input formats: accept
// and reject for each text format, the two header formats, then the binary
// tier with hand-built bytes.
// WHY: format is not cosmetic. Each format has signature behaviors (CSV's
// bare-vs-quoted empty field, RBWD's marker byte, Native's silent CAST,
// Buffers' silent reinterpret) that change what the table ends up holding.
// LOOK FOR: the same logical row giving format-specific verdicts, and the
// byte-level payloads in the comments: every binary payload is explained.
// C API: chs_preview_row / chs_preview_batch with the Format values (frozen
// integers: JSONEachRow=0 ... TSVWithNames=11).
// ---------------------------------------------------------------------------
func section7(lib *chtypes.Library, reg *chtypes.Registry) {
	section(7, "One schema, every format")
	kv("schema", formatDDL)
	blank()

	s, err := lib.CompileTable(formatDDL)
	if err != nil {
		fatal("%v", err)
	}
	defer s.Close()

	group("text formats (one row per line; positional or named)")
	feed(s, "JSONEachRow  accept", chtypes.JSONEachRow, []byte(`{"device_id":1,"seq":7,"label":"ok"}`))
	feed(s, "JSONEachRow  reject", chtypes.JSONEachRow, []byte(`{"device_id":"abc"}`))
	feed(s, "CSV          accept", chtypes.CSV, []byte(`1,7,ok`))
	feed(s, "CSV          reject", chtypes.CSV, []byte(`x,7,ok`))
	feed(s, "TSV          accept", chtypes.TSV, []byte("1\t7\tok"))
	feed(s, "TSV          reject", chtypes.TSV, []byte("y\t7\tok"))
	feed(s, "JSONCompact  accept", chtypes.JSONCompactEachRow, []byte(`[1,7,"ok"]`))
	feed(s, "JSONCompact  reject", chtypes.JSONCompactEachRow, []byte(`["z",7,"ok"]`))
	feed(s, "Values       accept", chtypes.Values, []byte(`(1,7,'ok')`))
	feed(s, "Values       DEFAULT", chtypes.Values, []byte(`(3,DEFAULT,'d')`))
	feed(s, "Values       decline", chtypes.Values, []byte(`(2,7,concat('a','b'))`))
	note("Values has an explicit DEFAULT keyword; an SQL expression is a")
	note("DECLINE (section 5c): evaluated by a real server, guessed by nobody")
	blank()

	// CSV's signature rule deserves its own two lines.
	kv("CSV empty-field rule", "bare empty takes the DEFAULT; quoted empty is ''")
	feed(s, "CSV   bare   2,7,", chtypes.CSV, []byte(`2,7,`))
	feed(s, `CSV   quoted 3,7,""`, chtypes.CSV, []byte(`3,7,""`))
	blank()

	group("header formats (the first row NAMES the columns)")
	feed(s, "CSVWithNames accept", chtypes.CSVWithNames, []byte("device_id,seq,label\n1,7,ok"))
	feed(s, "CSVWithNames reorder", chtypes.CSVWithNames, []byte("label,device_id,seq\nok,1,7"))
	feed(s, "TSVWithNames accept", chtypes.TSVWithNames, []byte("device_id\tseq\tlabel\n1\t7\tok"))
	note("the reordered header stores the SAME row: the data is addressed by")
	note("NAME, so wire order is the producer's business, not the table's")
	feed(s, "CSVWithNames CASE", chtypes.CSVWithNames, []byte("DEVICE_ID,seq,label\n1,7,ok"))
	note("header-name matching is EXACT through 26.4 and case-insensitive from")
	note("26.5, so DEVICE_ID binds on one line and is an unknown field on the")
	note("other: the library's answer, not this SDK's")
	blank()

	group("binary formats (bytes, COUNTED, never NUL-terminated)")
	kv("  RowBinary payload", rowBinaryOK)
	feed(s, "RowBinary    accept", chtypes.RowBinary, unhex(rowBinaryOK))
	note("4-byte LE UInt32 (1), 1-byte UInt8 (7), varint-length String (\"ok\")")
	note("with no framing, no names, no self-description")
	feed(s, "RowBinary    reject", chtypes.RowBinary, []byte{0x01, 0x00})
	note("truncated mid-row: framing faults are all-or-nothing per batch")
	kv("  RBWD payload", rbwdMarker)
	feed(s, "RBWD  marker byte", chtypes.RowBinaryWithDefaults, unhex(rbwdMarker))
	note("RowBinaryWithDefaults prefixes each column with a marker byte:")
	note("00 = 'value follows', nonzero = 'compute the DEFAULT, read no value")
	note("bytes'. Above, seq's marker is 01 -> stored 7 (its DEFAULT).")
	kv("  RBWNTD payload", "(hand-built: LEB128 count, names, types, then a marker row)")
	feed(s, "RBWNTD       accept", chtypes.RowBinaryWithNamesAndTypesAndDefaults, unhex(rbwntdRow))
	note("on a line that lacks the format, the line above is the server's own")
	note("UNKNOWN_FORMAT, not a chtypes error; section 12 turns exactly this")
	note("into the version-pinning lesson")
	blank()

	group("Native: self-describing, and it CASTs")
	feed(s, "Native       accept", chtypes.Native, unhex(nativeOK))
	note("a Native block declares its OWN column names and types (captured")
	note("from a live 25.8 SELECT ... FORMAT Native)")
	feed(s, "Native       CAST", chtypes.Native, unhex(nativeCast))
	note("this block declares seq as String \"200\" while the table says UInt8:")
	note("the disagreement is CAST silently (input_format_native_allow_types_")
	note("conversion defaults to true), visible here as a Transform, invisible")
	note("on a real server")
	blank()

	group("Buffers: NO self-description at all")
	newest, err := reg.For(newestLine(reg))
	if err != nil {
		return
	}
	sn, err := newest.CompileTable(tableOf("x Int32"))
	if err != nil {
		return
	}
	defer sn.Close()
	kv("  library", newest.Minor+"  (Buffers arrives at 26.5; this block uses the newest installed)")
	kv("  payload", "4 bytes ff ff ff ff, declared x Int32")
	feed(sn, "Buffers reinterpret", chtypes.Buffers, unhex(buffersOK))
	note("a UInt32 producer's 4294967295 reads back as -1: same width, no")
	note("metadata, so no check CAN fire. The over-accept class in a format")
	note("that cannot detect it: the schema is entirely out of band.")
	feed(sn, "Buffers width", chtypes.Buffers, unhex(buffersWide))
	note("the same 4 bytes declared 8 wide IS caught: size accounting disagrees")
}

// ---------------------------------------------------------------------------
// SECTION 8 — Engines, MergeTree settings, and TTL
//
// WHAT: declare the table's engine, settings and TTL IN the statement, then
// watch the STORAGE layer change what a batch stores.
// WHY: a row can be accepted per row and absent per batch. SummingMergeTree
// folds rows at insert, so a gateway reading only per-row verdicts previews
// rows the table will never hold. A TTL is different: it deletes at the next
// MERGE, which a preview does not perform, so EngineRows matches the INSERT.
// LOOK FOR: EngineRows (the stored truth) being SHORTER than the input for
// SummingMergeTree; the TTL batch whose row is accepted and whose EngineRows
// still HOLDS it (before build 20261007.120436 it was empty and a ttl_expired
// transform was reported); and the refusal-vs-decline pair on MergeTree
// settings (the class decides which).
// C API: chs_schema_create (the engine is part of the statement),
// chs_preview_batch.
// ---------------------------------------------------------------------------
func section8(lib *chtypes.Library) {
	section(8, "Engines, MergeTree settings, and TTL")

	// (a) A specialized engine changes what the table STORES.
	kv("(a) engine", "SummingMergeTree ORDER BY (day, key)")
	s, err := lib.CompileTable("CREATE TABLE t (day Date, key UInt32, v UInt64) ENGINE = SummingMergeTree ORDER BY (day, key)")
	if err != nil {
		kv("  failed", errStr(err))
		return
	}
	body := []byte(`{"day":"2026-01-01","key":1,"v":5}` + "\n" + `{"day":"2026-01-01","key":1,"v":7}`)
	b, err := s.Rows(chtypes.JSONEachRow, body)
	if err != nil {
		fatal("%v", err)
	}
	kv("  rows in / rows_read", fmt.Sprintf("2 / %d   (v=5 and v=7, same key)", b.RowsRead))
	kv("  EngineRows (stored)", engineRows(b.EngineRows))
	note("two rows in, ONE row out, v summed: EngineRows is the post-merge")
	note("preview and, when present, the truth to believe over Rows")
	s.Close()
	blank()

	// (b) MergeTree-namespace settings are part of the statement. Two
	// failures, two KINDS: the class decides, a refusal is the SERVER's, a
	// decline is this LIBRARY declining. Never flatten them. A setting only
	// the storage layer reads is accepted: the rows stored do not change.
	kv("(b) MergeTree settings", "refusal vs decline vs accepted")
	for _, c := range []struct{ label, setting string }{
		{"unknown NAME", "index_granularityy = 8192"},
		{"read on insert", "index_granularity = 4096"},
		{"storage-only", "old_parts_lifetime = 100"},
		{"known, AT default", "index_granularity = 8192"},
	} {
		s2, err := lib.CompileTable("CREATE TABLE t (a UInt8) ENGINE = MergeTree ORDER BY tuple() SETTINGS " + c.setting)
		kv("  "+c.label, c.setting+" -> "+okOr(err, "compiled"))
		if err != nil {
			note(truncate(classify(err), 90))
		}
		if s2 != nil {
			s2.Close()
		}
	}
	note("an unknown name is the server's own refusal: this DDL can never")
	note("exist, tell the tenant. A decline means the library will not guess:")
	note("a real server might well accept it, so validate cautiously. A")
	note("setting only the storage layer reads compiles: the rows stored do")
	note("not change.")
	blank()

	// (c) TTL: accepted per row, and still written by the INSERT.
	ttlDDL := "CREATE TABLE t (ts DateTime, v UInt8) ENGINE = MergeTree ORDER BY ts TTL ts + INTERVAL 1 DAY"
	s5, err := lib.CompileTable(ttlDDL)
	if err != nil {
		fatal("%v", err)
	}
	defer s5.Close()
	kv("(c) TTL", "ts + INTERVAL 1 DAY")
	bt, err := s5.Rows(chtypes.JSONEachRow, []byte(`{"ts":"2020-01-01 00:00:00","v":9}`))
	if err != nil {
		fatal("%v", err)
	}
	kv("  row fed", `{"ts":"2020-01-01 00:00:00","v":9}`)
	if len(bt.Rows) > 0 {
		kv("  row-level outcome", string(bt.Rows[0].Outcome)+"   <- the row PARSED fine")
	}
	kv("  EngineRows (stored)", engineRows(bt.EngineRows))
	for _, t := range bt.Transformed {
		kv("  batch transform", fmt.Sprintf("row=%d column=%q reason=%s lossy=%v", t.Row, t.Column, t.Reason, t.Lossy))
	}
	note("accepted per ROW, and EngineRows keeps it: a 2020 timestamp is already")
	note("past a one-day TTL, but a server's INSERT still writes the row and the")
	note("next merge deletes it, which a preview does not perform. On builds")
	note("before 20261007.120436 EngineRows was empty and a ttl_expired")
	note("transform was reported; the loop above now prints nothing.")
	blank()

	// (d) A TTL this library will not guess at.
	_, err = lib.CompileTable("CREATE TABLE t (ts DateTime, v UInt8) ENGINE = MergeTree ORDER BY ts TTL now() + INTERVAL 1 DAY")
	kv("(d) TTL now()+1 DAY", classify(err))
	note(truncate(errStr(err), 90))
	note("a clock-reading TTL is answered by the library per its own rules;")
	note("a decline here means it was not guessed")
}

// ---------------------------------------------------------------------------
// SECTION 9 — Settings precedence: who wins
//
// WHAT: the same row and the same table, answered differently as settings are
// supplied at each layer:
//
//	per-call  >  compile profile  >  process defaults (Setup)  >  ClickHouse's own
//
// WHY: this is how a gateway declares a deployment's settings ONCE (at
// compile) yet still lets one INSERT override per call. If precedence were
// fuzzy, the declared profile would not actually be in force.
// LOOK FOR: three verdicts on one row: the process default ("basic") rejects
// an ISO-8601 timestamp, the profile ("best_effort") overrides that default
// and accepts, and a per-call "basic" overrides the profile and rejects again.
// C API: chs_set_defaults (fixed at setup), chs_schema_create (the profile),
// chs_preview_row (the call's settings).
// ---------------------------------------------------------------------------
func section9(lib *chtypes.Library) {
	section(9, "Settings precedence: who wins")
	kv("the probe", `ts DateTime  fed  {"ts":"2026-01-15T10:30:00Z"}`)
	kv("process defaults", fmt.Sprintf("%v   (chtypes.Setup, fixed in main before any library opened)", setupDefaults))
	note("stock ClickHouse parses 'basic' datetimes only; best_effort accepts")
	note("ISO-8601, so the verdict TELLS you which setting value won")
	blank()

	iso := []byte(`{"ts":"2026-01-15T10:30:00Z"}`)
	basic := chtypes.WithSettings(map[string]string{"date_time_input_format": "basic"})
	bestEffort := chtypes.WithSettings(map[string]string{"date_time_input_format": "best_effort"})

	plain, err := lib.CompileTable(tableOf("ts DateTime"))
	must(err)
	defer plain.Close()
	profiled, err := lib.CompileTable(tableOf("ts DateTime"),
		chtypes.WithSettings(map[string]string{"date_time_input_format": "best_effort"}))
	must(err)
	defer profiled.Close()

	r1, _ := plain.Rows(chtypes.JSONEachRow, iso)
	kv("1. process default (basic)", verdict(r1))
	note("nothing declared on the table or the call: the Setup default decides")
	r2, _ := profiled.Rows(chtypes.JSONEachRow, iso)
	kv("2. compile profile best_effort", verdict(r2))
	note("the profile declared at COMPILE reaches every later call and")
	note("overrides the process default beneath it")
	r3, _ := profiled.Rows(chtypes.JSONEachRow, iso, basic)
	kv("3.  + per-call basic", verdict(r3))
	note("2 vs 3 is the requirement, measured: the same row PASSES under the")
	note("profile and FAILS when the per-call value overrides it")
	r4, _ := plain.Rows(chtypes.JSONEachRow, iso, bestEffort)
	kv("4. default + per-call best_effort", verdict(r4))
	note("a per-call value outranks the process default too")
	blank()

	kv("defaults are fixed", "there is no setter during traffic")
	err = chtypes.Setup(chtypes.SetupOptions{Defaults: map[string]string{"date_time_input_format": "best_effort"}})
	kv("  Setup with other defaults", classify(err))
	note("a different setup after the first is a *UsageError naming both; the")
	note("first stands. A binding never rewrites a value: settings are strings.")
}

// ---------------------------------------------------------------------------
// SECTION 10 — The error taxonomy
//
// WHAT: every kind of answer this SDK gives, told apart BY TYPE, never by
// string matching.
// WHY: the kinds demand different reactions (tell the tenant / fall back
// cautiously / fix the caller / fix the deployment), and Go's error types
// are deliberately PEERS: a decline can never satisfy errors.As against the
// refusal type, so a caller handling only refusals cannot silently convert
// declines into rejections.
// LOOK FOR: the errors.As switch you would write in production, and the
// reminder that ROW verdicts are data (RowResult.Outcome), not errors.
// ---------------------------------------------------------------------------
func section10(lib *chtypes.Library) {
	section(10, "The error taxonomy")

	kv("the Go idiom", "errors.As against four peer call types, one artifact type")
	raw("      var se *chtypes.SchemaError       // CHS_REJECTED: ClickHouse's own refusal")
	raw("      var ue *chtypes.UnsupportedError  // CHS_DECLINED: a decline")
	raw("      var us *chtypes.UsageError        // CHS_INVALID_ARGUMENT: a misuse")
	raw("      var ie *chtypes.InternalError     // CHS_INTERNAL: a library fault")
	raw("      var ae *chtypes.ArtifactError     // a fetch or load failure, with a shared code")
	blank()

	// A REFUSAL: ClickHouse's own code rides on *SchemaError.
	_, err := lib.CompileTable(tableOf("x NotAType"))
	describeError(err, "compile x NotAType")

	// A DECLINE: *UnsupportedError, no ClickHouse code to carry.
	_, err = lib.CompileTable("CREATE TABLE t (ts DateTime, v UInt8) ENGINE = MergeTree ORDER BY ts TTL now() + INTERVAL 1 DAY")
	describeError(err, "TTL now()+1 DAY")

	// A MISUSE: a closed handle is refused before any C call.
	s, _ := lib.CompileTable(tableOf("a UInt8"))
	s.Close()
	_, err = s.Describe()
	describeError(err, "Describe on a closed schema")

	// An ARTIFACT error: a fetch or load failure, one shared code.
	blank()
	_, err = chtypes.OpenUnverified("/nonexistent/libchtypes.so", false)
	describeError(err, "OpenUnverified without allow")
	reg, _ := chtypes.NewRegistry()
	_, err = reg.For("99.9")
	describeError(err, "For(99.9)")

	blank()
	kv("row verdicts are DATA", "RowResult.Outcome, not an error return")
	note("a row the server would reject comes back (RowResult, nil): the")
	note("error return is about whether the question could be asked; the")
	note("verdict (accepted / rejected / accepted_poisoned / skipped /")
	note("unsupported) lives in the answer.")
	kv("error codes", "ArtifactError.Code is one of ten shared codes; the CLI's exit status is theirs")
}

// ---------------------------------------------------------------------------
// SECTION 11 — The discovery kit, offline
//
// WHAT: the library's own discovery query, its column reader, and the
// settings a deployment changed, run here against CANNED bytes shaped exactly
// like a real server's JSONEachRow answer.
// WHY: chtypes NEVER opens a socket. You run the query with whatever client
// you already have; the library gives you the SQL and reads the result. The
// payoff is the last step: the server's own version resolves a library, and
// the discovered settings become the compile profile, so the handle behaves
// like a table created on THAT deployment.
// LOOK FOR: the declarations in the server's own spelling (DEFAULTs carried,
// the odd column name quoted), and the SAME ROW accepted under the discovered
// profile but rejected under a stock compile.
// C API: chs_discover_query, chs_discover_columns, then the compile. (The
// ONLINE flow, against a real server, is ingest-demo/.)
// ---------------------------------------------------------------------------
func section11(reg *chtypes.Registry, base *chtypes.Library) {
	section(11, "The discovery kit, offline")
	kv("NOTE", "the response below is CANNED: shaped exactly like a real")
	kv("", "server's, so the reader cannot tell. Swap in your HTTP client.")
	blank()

	// Step 1: the library's own query. The caller binds {database:String} and
	// {table:String} as query parameters when it runs it.
	query, err := base.DiscoverQuery()
	if err != nil {
		fatal("%v", err)
	}
	kv("DiscoverQuery", truncate(query, 70))
	kv("  you bind", "param_database, param_table as query parameters")
	for _, l := range strings.Split(strings.TrimSpace(cannedColumnsResult), "\n") {
		kv("  canned response", l)
	}
	blank()

	// Step 2: ClickHouse's own reader turns the rows into declarations.
	// Which library reads them does not matter; the one for the server's
	// version is used below.
	kv("server version", cannedServerVersion+"  (the CALLER supplies it in v1: nothing here asks a server)")
	lib, err := reg.For(lineOfVersion(cannedServerVersion))
	if err != nil {
		kv("registry.For("+lineOfVersion(cannedServerVersion)+")", errStr(err))
		note("this line is not installed (`chtypes fetch " + lineOfVersion(cannedServerVersion) + "` would add it);")
		note("the rest of this section uses the library already open")
		lib = base
	} else {
		kv("registry.For("+lineOfVersion(cannedServerVersion)+")", "library "+lib.Version+"  (the "+lib.Minor+" line)")
	}
	disc, err := lib.DiscoverColumns([]byte(cannedColumnsResult))
	if err != nil {
		fatal("%v", err)
	}
	for i, c := range disc.Columns {
		kv(fmt.Sprintf("  [%d] %s", i, c.Name), c.Declaration)
	}
	kv("  ColumnsSQL", truncate(disc.ColumnsSQL, 80))
	note("default_kind and default_expression are CARRIED: dropping them would")
	note("silently lose the DEFAULT semantics sections 6 and 8 run on. The")
	note("column NAME is spelled by ClickHouse's own quoting, not by a rule here.")
	blank()

	// Step 3: the payoff. Declared settings become the compile profile. They
	// are the CALLER's to supply: v1 has no call that asks a server for them.
	profile := map[string]string{"date_time_input_format": "best_effort"}
	ddl := "CREATE TABLE t (" + disc.ColumnsSQL + ") ENGINE = MergeTree ORDER BY tuple()"
	sProf, err := lib.CompileTable(ddl, chtypes.WithSettings(profile))
	if err != nil {
		fatal("%v", err)
	}
	defer sProf.Close()
	sPlain, err := lib.CompileTable(ddl)
	if err != nil {
		fatal("%v", err)
	}
	defer sPlain.Close()
	row := []byte(`{"ts":"2026-01-15T10:30:00Z","device_id":9,"reading c":21.5}`)
	kv("the same row, twice", string(row))
	bProf, _ := sProf.Rows(chtypes.JSONEachRow, row)
	kv("  under the discovered profile", verdict(bProf))
	bPlain, _ := sPlain.Rows(chtypes.JSONEachRow, row)
	kv("  under a stock compile", verdict(bPlain))
	note("the deployment declared date_time_input_format=best_effort, so ITS")
	note("server takes the ISO-8601 timestamp; a stock compile answers for a")
	note("server the tenant does not have. Discovery is what closes that gap.")
}

// ---------------------------------------------------------------------------
// SECTION 12 — Version pinning: same input, different answers
//
// WHAT: the same statement and the same bytes, swept across every installed
// line in this process.
// WHY: version differences are the reason the registry exists. They are not
// monotonic (newer is NOT always more permissive), so no rule can predict
// them; only the real per-version library can answer.
// LOOK FOR: one line rejecting a DEFAULT that others accept; and the Buffers
// format simply not existing before 26.5 (the server's own refusal).
// ---------------------------------------------------------------------------
func section12(reg *chtypes.Registry) {
	section(12, "Version pinning: same input, different answers")
	installed, _ := reg.Installed()
	lines := installedLines(installed)
	if len(lines) < 2 {
		kv("lines installed", strings.Join(lines, "  "))
		note("only one line is installed, so there is nothing to sweep: the")
		note("point of this section needs at least two. Fetch another")
		note("(`chtypes fetch 26.7`) and re-run to see the answers diverge.")
		return
	}

	kv("(a) a mixed-type DEFAULT", "a UInt8, x Int64 DEFAULT if(1,2,'a')")
	for _, v := range lines {
		lib, err := reg.For(v)
		if err != nil {
			kv("  "+v, "SKIPPED  "+errStr(err))
			continue
		}
		s, err := lib.CompileTable(tableOf("a UInt8, x Int64 DEFAULT if(1,2,'a')"))
		if err != nil {
			var se *chtypes.SchemaError
			if errors.As(err, &se) {
				kv("  "+v, fmt.Sprintf("REJECTED code %d  %s", se.ChCode, truncate(se.Message, 52)))
			} else {
				kv("  "+v, errStr(err))
			}
			continue
		}
		d, _ := s.Describe()
		kv("  "+v, "compiled  ("+d.Columns[1].Type+" DEFAULT "+d.Columns[1].DefaultExpr+")")
		s.Close()
	}
	note("NEWER IS NOT ALWAYS MORE PERMISSIVE: no monotonic rule predicts")
	note("this, which is exactly why one real library per line exists")
	blank()

	kv("(b) a format's arrival", "Buffers (added in ClickHouse 26.5)")
	kv("  payload", "1 column, 1 row, 4 bytes ff ff ff ff, declared x Int32")
	for _, v := range lines {
		lib, err := reg.For(v)
		if err != nil {
			kv("  "+v, "SKIPPED  "+errStr(err))
			continue
		}
		s, err := lib.CompileTable(tableOf("x Int32"))
		if err != nil {
			continue
		}
		b, err := s.Rows(chtypes.Buffers, unhex(buffersOK))
		switch {
		case err != nil:
			kv("  "+v, "call failed: "+err.Error())
		case b.Outcome == chtypes.Accepted && len(b.Rows) > 0 && len(b.Rows[0].Values) > 0:
			kv("  "+v, "accepted  x = "+b.Rows[0].Values[0].Text)
		default:
			kv("  "+v, fmt.Sprintf("%v  code=%d  %s", b.Outcome, b.ErrCode, truncate(b.ErrMsg, 40)))
		}
		s.Close()
	}
	note("the refusal on the older lines is the SERVER'S own answer: ask the")
	note("library (one payload through Rows, or capabilities.input_formats)")
	note("instead of trusting your own version arithmetic")
}

// ---------------------------------------------------------------------------
// SECTION 13 — Teardown
//
// WHAT: what to release, and when.
// WHY: schema, filter and block handles are C allocations (freed by Close, and
// by a finalizer when abandoned). No library is ever unloaded, so a registry
// and a library own nothing to close.
// C API: chs_schema_free, chs_filter_free, chs_block_free.
// ---------------------------------------------------------------------------
func section13() {
	section(13, "Teardown")
	kv("schema, filter, block", "Close() each when done; Close is idempotent, any order is safe")
	kv("Registry, Library", "no Close: nothing to release")
	note("a library is never unloaded: there is no dlclose in any binding, and")
	note("chs_shutdown is never called, so an ordinary process owes nothing.")
	note("A filter or a block holds a counted reference to its schema inside")
	note("the library, so closing a schema while its filters are in use is")
	note("legal and they keep working. A finalizer frees what the caller")
	note("abandons; using a closed object is a *UsageError raised before any C call.")
}

// ---------------------------------------------------------------------------
// SECTION 15 — Export: bytes + spans
//
// WHAT: the SAME call that judges a batch can also serialize its accepted
// rows to wire bytes (here JSONCompactEachRow), addressed per row by
// index-aligned spans, one C call, never a second.
// WHY: every consumer of an accepted row wants the stored bytes ready to
// publish or INSERT without rebuilding them from the document: reassembly is
// where caller bugs live (the invalid-JSON-on-poisoned-rows class), and the
// export carries every generated DEFAULT already filled in.
// LOOK FOR: the skipped row's {0,0} span; a span slice BEING the row's line;
// the poisoned batch DECLINING the export and saying why (fail-closed: an
// unreadable value cannot be honestly serialized); emitted-EMPTY (an answer)
// vs declined (not one); and the lean document (doc flags) keeping every
// verdict while dropping the description.
// C API: chs_preview_batch with export_format and doc_flags.
// ---------------------------------------------------------------------------
func section15(lib *chtypes.Library) {
	section(15, "Export: bytes + spans")

	cs, err := lib.CompileTable(formatDDL)
	must(err)
	defer cs.Close()

	body := []byte(`{"device_id":1,"label":"ok"}` + "\n" +
		`{"device_id":"zap"}` + "\n" +
		`{"device_id":3,"label":"hi"}` + "\n")
	allowErrors := chtypes.WithSettings(map[string]string{"input_format_allow_errors_num": "10"})
	res, err := cs.Rows(chtypes.JSONEachRow, body, allowErrors,
		chtypes.WithExport(chtypes.JSONCompactEachRow), chtypes.WithDocFlags(chtypes.DocAll))
	must(err)
	if res.Outcome == chtypes.Unsupported {
		kv("Rows with export", "declined: "+truncate(res.ErrMsg, 64))
		note("this build declines the export request; the section degrades here")
		return
	}
	kv("batch", fmt.Sprintf("%s  rows_read=%d rows_skipped=%d", res.Outcome, res.RowsRead, res.RowsSkipped))
	kv("payload", fmt.Sprintf("%q  (%d bytes, one line per ACCEPTED row)", res.Payload, len(res.Payload)))
	note("wire order = declared minus MATERIALIZED/ALIAS/EPHEMERAL, so these")
	note("bytes are directly INSERT-able with no column list; DEFAULTs (seq=7,")
	note("label='unknown') are already applied: preview == stored")
	for i, sp := range res.Spans {
		l := "(no bytes: row not accepted)"
		if sp.Len > 0 {
			l = fmt.Sprintf("%q", res.Payload[sp.Off:sp.Off+sp.Len])
		}
		kv(fmt.Sprintf("  span[%d] {off:%d len:%d}", i, sp.Off, sp.Len), fmt.Sprintf("%s -> %s", res.Rows[i].Outcome, l))
	}
	note("spans are INDEX-ALIGNED with rows; slicing spans out of the payload")
	note("IS the per-row payload, and concatenating non-zero spans reproduces")
	note("it exactly: batches merge by byte concatenation")
	blank()

	// The lean document: fewer doc flags keep the whole verdict channel and
	// drop the description: same bytes, thinner JSON.
	lean, err := cs.Rows(chtypes.JSONEachRow, body, allowErrors,
		chtypes.WithExport(chtypes.JSONCompactEachRow), chtypes.WithDocFlags(0))
	must(err)
	vals := 0
	if len(lean.Rows) > 0 {
		vals = len(lean.Rows[0].Values)
	}
	kv("lean (flags 0)", fmt.Sprintf("outcome %s, %d verdict rows, %d Values, %d Transformed, payload identical: %v",
		lean.Outcome, len(lean.Rows), vals, len(lean.Transformed), string(lean.Payload) == string(res.Payload)))
	note("flags thin the DESCRIPTION, never the VERDICT; DocValues /")
	note("DocTransforms / DocDefaults pick groups a la carte")
	blank()

	// Fail-closed: a poisoned batch holds a value ClickHouse itself cannot
	// read back; no writer can honestly serialize it, so no bytes.
	ps, err := lib.CompileTable(tableOf("e Enum8('a' = 1, 'b' = 2)"))
	must(err)
	defer ps.Close()
	poi, err := ps.Rows(chtypes.JSONEachRow, []byte(`{"e":null}`+"\n"),
		chtypes.WithSettings(map[string]string{"input_format_defaults_for_omitted_fields": "0"}),
		chtypes.WithExport(chtypes.JSONCompactEachRow), chtypes.WithDocFlags(chtypes.DocAll))
	must(err)
	pv := "nil (declined)"
	if poi.Payload != nil {
		pv = fmt.Sprintf("%d bytes", len(poi.Payload))
	}
	kv("poisoned batch", fmt.Sprintf("%s -> payload=%s export_declined=%q", poi.Outcome, pv, truncate(poi.ExportDeclined, 48)))

	// Emitted-empty is an ANSWER (zero accepted rows), not a decline.
	emp, err := cs.Rows(chtypes.JSONEachRow, nil, chtypes.WithExport(chtypes.JSONCompactEachRow))
	if err != nil {
		kv("empty batch", classify(err))
	} else {
		kv("empty batch", fmt.Sprintf("%s  payload non-nil=%v len=%d", emp.Outcome, emp.Payload != nil, len(emp.Payload)))
	}
	note("an empty input block stays a decline; an accepted batch of zero rows")
	note("emits an empty, non-nil payload, which is an answer")
}

// ---------------------------------------------------------------------------
// SECTION 16 — Filters: WHERE semantics at the edge
//
// WHAT: compile one boolean expression against a schema (CompileFilter) and
// evaluate it per row of a body: ClickHouse's own comparison functions, so
// the answers are WHERE-side by construction.
// WHY: read-side row visibility (who may SEE this row) is a WHERE question,
// and WHERE coercion is NOT insert coercion: `x = 256` over UInt8 PROMOTES
// (false for every row) where an insert would wrap 256 to 0.
// LOOK FOR: 'f','f' where the insert path stores 0; NULL being not-true; the
// 'e' class (compiles, then THROWS per row: a server fails the WHOLE query
// here); clock reads and unbound {p:Type} parameters REFUSED at compile; the
// query-parameter and block-twin sub-demos; and the enforcement gate at the
// end.
// C API: chs_filter_create, chs_filter_eval_body, chs_filter_eval_block,
// chs_block_create, and the three frees.
// ---------------------------------------------------------------------------
func section16(lib *chtypes.Library) {
	section(16, "Filters: WHERE semantics at the edge")

	cs, err := lib.CompileTable(tableOf("x UInt8"))
	must(err)
	defer cs.Close()

	f, err := cs.CompileFilter("x = 256")
	must(err)
	fr, err := f.Rows(chtypes.JSONEachRow, []byte(`{"x":0}`+"\n"+`{"x":255}`+"\n"))
	must(err)
	kv("filter `x = 256` over UInt8", "verdicts "+verdictString(fr))
	note("PROMOTES, never wraps: false for x=0 AND x=255. The insert side of")
	note("this same library stores 256 as 0 (section 5's overflow_wrap), which")
	note("is why predicate constants must never be folded through insert coercion")
	f.Close()
	blank()

	// NULL is not true: three-valued logic collapsed at the WHERE boundary.
	ns, err := lib.CompileTable(tableOf("lvl Nullable(UInt8)"))
	must(err)
	defer ns.Close()
	nf, err := ns.CompileFilter("lvl = 1")
	must(err)
	fr, err = nf.Rows(chtypes.JSONEachRow, []byte(`{"lvl":null}`+"\n"+`{"lvl":1}`+"\n"))
	must(err)
	kv("`lvl = 1` on [null, 1]", "verdicts "+verdictString(fr)+"   (NULL is not true, as WHERE hides it)")
	nf.Close()
	blank()

	// The 'e' class: compiles clean, then THROWS on every row's values.
	ss, err := lib.CompileTable(tableOf("s String"))
	must(err)
	defer ss.Close()
	sf, err := ss.CompileFilter("s = 257")
	must(err)
	fr, err = sf.Rows(chtypes.JSONEachRow, []byte(`{"s":"hi"}`+"\n"))
	must(err)
	kv("`s = 257` over String", "verdicts "+verdictString(fr))
	for _, fe := range fr.Errors {
		kv(fmt.Sprintf("  row %d", fe.Row), fmt.Sprintf("code %d  %s", fe.Code, truncate(fe.Msg, 56)))
	}
	note("on a real server this WHERE fails the WHOLE query: 'e' is NOT an")
	note("answer, and neither is 'd' (a row this library declines): an")
	note("enforcing caller fails CLOSED on both (Verdict.Answered is false),")
	note("or NOT(decline-as-false) inverts fail-closed into fail-open")
	sf.Close()
	blank()

	// A bad row declines ('d'), itemized, and the tail keeps its indexes.
	df, err := cs.CompileFilter("x < 5")
	must(err)
	fr, err = df.Rows(chtypes.JSONEachRow, []byte(`{"x":1}`+"\n"+`{"x":"zap"}`+"\n"+`{"x":9}`+"\n"))
	must(err)
	kv("`x < 5` on [1, bad, 9]", "verdicts "+verdictString(fr)+"   (the bad row cannot swallow the tail)")
	df.Close()
	blank()

	// Refused at compile, never guessed.
	_, err = cs.CompileFilter("now() > x")
	kv("compile `now() > x`", classify(err))
	_, err = cs.CompileFilter("x = {p:UInt8}")
	kv("compile `x = {p:UInt8}`", classify(err))
	note("clock reads would be answered with THIS process's clock, not the")
	note("server's; an UNBOUND {p:Type} is the SERVER's own error: bind it")
	_, err = cs.CompileFilter("nosuch = 1")
	kv("compile `nosuch = 1`", classify(err))
	blank()

	// Query parameters.
	group("query parameters: values are STRINGS, never escaped")
	ps, err := lib.CompileTable(tableOf("tenant String, role String, x UInt8"))
	must(err)
	defer ps.Close()
	eventBody := []byte(`{"tenant":"acme","role":"admin","x":1}` + "\n" +
		`{"tenant":"evil","role":"viewer","x":2}` + "\n" +
		`{"tenant":"' OR 1=1 --","role":"admin","x":3}` + "\n")

	tf, err := ps.CompileFilter("tenant = {t:String}", chtypes.WithFilterParams(map[string]string{"t": "acme"}))
	must(err)
	fr, err = tf.Rows(chtypes.JSONEachRow, eventBody)
	must(err)
	kv("`tenant = {t:String}`, t=acme", "verdicts "+verdictString(fr))
	tf.Close()
	note("compiled ONCE per (schema, expr, params): the value is baked in;")
	note("a per-tenant cache MUST be a bounded LRU + a compile throttle")

	hostile := `' OR 1=1 --`
	hf, err := ps.CompileFilter("tenant = {t:String}", chtypes.WithFilterParams(map[string]string{"t": hostile}))
	must(err)
	fr, err = hf.Rows(chtypes.JSONEachRow, eventBody)
	must(err)
	hostileOK := fr.Outcome == chtypes.FilterOK && len(fr.Verdicts) == 3 &&
		fr.Verdicts[0] == chtypes.VerdictFalse && fr.Verdicts[1] == chtypes.VerdictFalse &&
		fr.Verdicts[2] == chtypes.VerdictTrue
	kv("t = `' OR 1=1 --` (hostile)", fmt.Sprintf("verdicts %s   hostile-value-inert: %v", verdictString(fr), hostileOK))
	hf.Close()
	note("the value became a typed LITERAL after SQL parsing: it matches only")
	note("the row holding exactly that string, and NOTHING was escaped to get")
	note("there (never hand-escape values). Size the {brace type} for the")
	note("value's domain, and never NAME a param `limit` or `offset`.")
	blank()

	// The block twin: parse once, evaluate K filters.
	group("the block twin: parse ONCE, evaluate K filters")
	blk, err := ps.ParseBlock(chtypes.JSONEachRow, eventBody)
	must(err)
	adminF, err := ps.CompileFilter("role = 'admin'")
	must(err)
	viewerF, err := ps.CompileFilter("role = 'viewer'")
	must(err)
	fr, err = adminF.Eval(blk)
	must(err)
	kv("eval `role = 'admin'`", "verdicts "+verdictString(fr))
	fr, err = viewerF.Eval(blk)
	must(err)
	kv("eval `role = 'viewer'`", "verdicts "+verdictString(fr))
	adminF.Close()
	viewerF.Close()
	blk.Close()
	note("ONE parse of the 3-row event fed BOTH filters: eval neither consumes")
	note("nor mutates the block, and Eval(ParseBlock(body)) is Rows(body) for")
	note("every verdict class. A filter and a block each hold a counted")
	note("reference to the schema, so Close order never matters.")
	blank()

	note("THREADS: concurrent calls on one handle are safe in the library, so")
	note("two filters over one schema may run at once")
	note("ENFORCEMENT GATE: nothing may enforce read-side security on this")
	note("API until the WHERE-truth rig gates green (zero over-admit, zero")
	note("over-hide). Until then this is a shadow/replay surface: log")
	note("disagreements, enforce with what enforced yesterday.")
}

// verdictString renders a FilterResult's verdicts as the document's compact
// t/f/e/d string.
func verdictString(fr chtypes.FilterResult) string {
	if fr.Outcome != chtypes.FilterOK {
		return fmt.Sprintf("(call-level: outcome=%v code=%d %s)", fr.Outcome, fr.ErrCode, truncate(fr.ErrMsg, 40))
	}
	var b strings.Builder
	for _, v := range fr.Verdicts {
		b.WriteString(string(v))
	}
	return "\"" + b.String() + "\""
}

// ---------------------------------------------------------------------------
// SECTION 17 — The INSERT column list
//
// WHAT: chtypes.WithColumns names the INSERT column list: the
// `INSERT INTO t (a, b, ...)` shape. The data then supplies exactly the
// listed columns, and the server computes the rest with them in scope.
// WHY: an EPHEMERAL column has NO value at all outside a column list: it
// exists only to feed another column's DEFAULT, so a gateway that never
// declares one can never reach it.
// LOOK FOR: a listed EPHEMERAL column's value reaching `d`'s DEFAULT (d = 6)
// while never appearing among the stored Values itself; and the SAME server
// code an unknown column and an ALIAS column in the list both answer.
// C API: chs_preview_row's columns argument.
// ---------------------------------------------------------------------------
func section17(lib *chtypes.Library) {
	section(17, "The INSERT column list")

	// e is EPHEMERAL: no value at all outside a column list. d's DEFAULT
	// reads e, so a caller that wants d computed from a supplied e must list
	// e explicitly.
	s, err := lib.CompileTable(tableOf("id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1"))
	must(err)
	defer s.Close()

	r, err := s.Row(chtypes.JSONEachRow, []byte(`{"id":3,"e":5}`), chtypes.WithColumns([]string{"id", "e"}))
	must(err)
	var vals []string
	for _, v := range r.Values {
		vals = append(vals, fmt.Sprintf("%s=%s(%s)", v.Column, textOr(v), v.Source))
	}
	kv("list (id, e), e=5", fmt.Sprintf("%s  %s", r.Outcome, strings.Join(vals, " ")))
	note("e was READ and was in scope for d's DEFAULT (d = e + 1 = 6), but e")
	note("is NOT among the Values above: a listed EPHEMERAL column is never")
	note("stored and never exported, exactly as if it did not exist to SELECT *")
	blank()

	// The refusal: an unknown name in the list. Names are never validated
	// locally: this is the server's own code, surfaced as it comes back (an
	// ALIAS column in the list answers the SAME code and message).
	r2, err := s.Row(chtypes.JSONEachRow, []byte(`{"id":1,"nosuch":2}`), chtypes.WithColumns([]string{"id", "nosuch"}))
	must(err)
	kv("list (id, nosuch)", fmt.Sprintf("%s  code=%d  %s", r2.Outcome, r2.ErrCode, truncate(r2.ErrMsg, 56)))
	note("NO_SUCH_COLUMN_IN_TABLE: indistinguishable on the wire from naming")
	note("an ALIAS column; a repeated name answers the server's own different")
	note("code (not shown). This library reimplements none of them.")
	blank()

	note("an absent or EMPTY list means exactly the same thing: today's no-list")
	note("behavior, and NEVER renders as `INSERT INTO t () ...`: that statement")
	note("is a syntax error on every server")
}

// ---------------------------------------------------------------- plumbing
//
// Everything below is printing helpers; no chtypes calls hide here beyond the
// small reads each one names.

func section(n int, title string) { fmt.Printf("\n=== %d. %s ===\n", n, title) }
func kv(k, v string)              { fmt.Printf("  %-30s %s\n", k, v) }
func note(s string)               { fmt.Printf("      . %s\n", s) }
func raw(s string)                { fmt.Println(s) }
func blank()                      { fmt.Println() }
func line(s string)               { fmt.Println(s) }
func group(t string)              { fmt.Printf("  -- %s\n", t) }

func fatal(f string, a ...any) {
	fmt.Fprintf(os.Stderr, "\nplayground: "+f+"\n", a...)
	os.Exit(1)
}

func must(err error) {
	if err != nil {
		fatal("%v", err)
	}
}

// feed runs one payload through Rows and prints the verdict on one line, plus
// a "~" line per Transform (a silent change ClickHouse made).
func feed(s *chtypes.Schema, label string, f chtypes.Format, body []byte) {
	b, err := s.Rows(f, body)
	if err != nil {
		kv("  "+label, "call failed: "+err.Error())
		return
	}
	parts := []string{string(b.Outcome)}
	if b.ErrCode != 0 {
		parts = append(parts, "code="+strconv.Itoa(int(b.ErrCode)))
	}
	if len(b.Rows) > 0 {
		var vals []string
		for _, v := range b.Rows[0].Values {
			vals = append(vals, fmt.Sprintf("%s=%s(%s)", v.Column, textOr(v), v.Source))
		}
		if len(vals) > 0 {
			parts = append(parts, strings.Join(vals, " "))
		}
	}
	if b.ErrMsg != "" {
		parts = append(parts, truncate(b.ErrMsg, 56))
	}
	kv("  "+label, strings.Join(parts, "  "))
	for _, t := range b.Transformed {
		lossy := ""
		if t.Lossy {
			lossy = ", LOSSY"
		}
		raw(fmt.Sprintf("      ~ %s: %s -> %s (%s%s)", t.Column, t.Input, t.Stored, t.Reason, lossy))
	}
}

// describeError prints which peer type an error is, with its fields.
func describeError(err error, what string) {
	var se *chtypes.SchemaError
	var ue *chtypes.UnsupportedError
	var us *chtypes.UsageError
	var ie *chtypes.InternalError
	var ae *chtypes.ArtifactError
	switch {
	case err == nil:
		kv(what, "(no error)")
	case errors.As(err, &ue):
		kv(what, "*UnsupportedError  (a DECLINE)")
		kv("  message", truncate(ue.Message, 84))
		kv("  also a *SchemaError?", fmt.Sprintf("%v   <- peers, not a hierarchy", errors.As(err, &se)))
	case errors.As(err, &se):
		kv(what, fmt.Sprintf("*SchemaError  (a REFUSAL), .ChCode=%d %s", se.ChCode, se.ChName))
		kv("  message", truncate(se.Message, 84))
		kv("  also an *UnsupportedError?", fmt.Sprintf("%v   <- peers, not a hierarchy", errors.As(err, &ue)))
	case errors.As(err, &us):
		kv(what, "*UsageError  (a MISUSE)")
		kv("  message", truncate(us.Message, 84))
	case errors.As(err, &ie):
		kv(what, "*InternalError  (a LIBRARY FAULT)")
		kv("  message", truncate(ie.Message, 84))
	case errors.As(err, &ae):
		kv(what, "*ArtifactError  .Code="+string(ae.Code))
		kv("  message", truncate(ae.Msg, 84))
		kv("  errors.Is(ErrArtifactMissing)", fmt.Sprintf("%v   <- one sentinel per code", errors.Is(err, chtypes.ErrArtifactMissing)))
	default:
		kv(what, fmt.Sprintf("%T: %v", err, err))
	}
}

// classify names an error's KIND in one word (used where the full describe
// would drown the section).
func classify(err error) string {
	var se *chtypes.SchemaError
	var ue *chtypes.UnsupportedError
	var us *chtypes.UsageError
	switch {
	case err == nil:
		return "accepted"
	case errors.As(err, &ue):
		return "DECLINED  (*UnsupportedError)"
	case errors.As(err, &se):
		return "REFUSED   (*SchemaError, code " + strconv.Itoa(int(se.ChCode)) + ")"
	case errors.As(err, &us):
		return "MISUSE    (*UsageError)"
	}
	return err.Error()
}

func verdict(b chtypes.BatchResult) string {
	if b.ErrCode != 0 {
		return fmt.Sprintf("%-9s code=%d  %s", b.Outcome, b.ErrCode, truncate(b.ErrMsg, 44))
	}
	if len(b.Rows) > 0 && len(b.Rows[0].Values) > 0 {
		return fmt.Sprintf("%-9s %s = %s", b.Outcome, b.Rows[0].Values[0].Column, b.Rows[0].Values[0].Text)
	}
	return string(b.Outcome)
}

func textOr(v chtypes.Value) string {
	if v.Text == "" && !v.Null {
		return "<unreadable>"
	}
	return v.Text
}

func engineRows(rows [][]chtypes.EngineCell) string {
	if rows == nil {
		return "(none reported)"
	}
	var out []string
	for _, r := range rows {
		var cells []string
		for _, c := range r {
			cells = append(cells, c.Column+"="+c.Text)
		}
		out = append(out, "["+strings.Join(cells, " ")+"]")
	}
	return fmt.Sprintf("%d rows %s", len(rows), strings.Join(out, " "))
}

func hasFeature(lib *chtypes.Library, name string) bool {
	for _, f := range lib.BuildInfo().Capabilities.Features {
		if f == name {
			return true
		}
	}
	return false
}

func or(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}

func okOr(err error, ok string) string {
	if err == nil {
		return ok
	}
	return truncate(err.Error(), 70)
}

func errStr(err error) string {
	if err == nil {
		return "(no error)"
	}
	return err.Error()
}

func truncate(s string, n int) string {
	s = strings.ReplaceAll(s, "\n", " ")
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func shortHash(s string) string {
	if len(s) > 10 {
		return s[:10]
	}
	return s
}

func unhex(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		panic(err)
	}
	return b
}

func hostPlatform() string { return runtime.GOOS + "-" + runtime.GOARCH }

// installedLines is the two-part spelling ("26.8") of every installed build
// for this host's platform, oldest line first. A line is the first two parts
// of a version the fetch layer reported; ordering them is this example's
// business, and only for printing.
func installedLines(installed []chtypes.Resolved) []string {
	seen := map[string]bool{}
	var lines []string
	for _, r := range installed {
		if r.Platform != hostPlatform() {
			continue
		}
		if l := lineOfVersion(r.Version); l != "" && !seen[l] {
			seen[l] = true
			lines = append(lines, l)
		}
	}
	sort.Slice(lines, func(i, j int) bool { return lineLess(lines[i], lines[j]) })
	return lines
}

func newestLine(reg *chtypes.Registry) string {
	installed, _ := reg.Installed()
	lines := installedLines(installed)
	if len(lines) == 0 {
		return ""
	}
	return lines[len(lines)-1]
}

func lineOfVersion(v string) string {
	parts := strings.Split(v, ".")
	if len(parts) < 2 {
		return ""
	}
	return parts[0] + "." + parts[1]
}

// lineLess orders two-part lines numerically: "25.10" > "25.3", which a
// string sort gets exactly backwards.
func lineLess(a, b string) bool {
	ap, bp := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < len(ap) && i < len(bp); i++ {
		x, _ := strconv.Atoi(ap[i])
		y, _ := strconv.Atoi(bp[i])
		if x != y {
			return x < y
		}
	}
	return len(ap) < len(bp)
}
