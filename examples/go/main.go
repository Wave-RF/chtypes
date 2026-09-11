// Command playground is the Go tour of chtypes.
//
// # WHAT THIS IS
//
// chtypes answers one question: "if this row were inserted into this table on
// this ClickHouse version, what would happen?" — without a server. The answers
// come from ClickHouse's own C++ (vendored per release into shared libraries
// behind a 22-function C ABI), which is why they are exact rather than
// approximately right.
//
// This file is a tutorial you RUN. Sixteen numbered sections walk the whole
// public API of the Go SDK, from loading an artifact to tearing down, each
// with a comment saying what it demonstrates, why an ingest pipeline cares,
// and what to look at in the output. The same sixteen sections — same
// numbering, same schemas, same rows — exist in python/demo.py, ts/demo.mjs
// and rust/src/main.rs, so you can diff two tours and see only the language
// idioms differ.
//
// EVERYTHING HERE IS OFFLINE. You need the Go toolchain and the artifacts in
// the registry (scripts/fetch.sh, or a core-repository build) — no Docker, no ClickHouse
// server, no network. Even the discovery-kit section (11) runs offline,
// against CANNED bytes shaped exactly like a real server's responses.
//
//	go run .                       # newest vendored version
//	CHTYPES_VERSION=25.8 go run .  # pick a line
//	../chplay.sh go                # same, with prerequisite checks
//
// Nothing here is a test — the real suites live in go/chtypes and
// tests/. Every number printed below is produced by the run, never written
// down by hand.
package main

import (
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"

	"github.com/wave-rf/chtypes/go/chtypes"
)

// ---------------------------------------------------------------- the fixture
//
// These constants are IDENTICAL in all four playgrounds. Change one here and
// you must change it in python/demo.py, ts/demo.mjs and rust/src/main.rs too —
// the point of this directory is that the four outputs can be diffed.

// The tenant's table for the DEFAULT/result sections: one of everything the
// tour needs — a volatile DEFAULT (now64), a literal DEFAULT, an Enum (how a
// table gets poisoned), and a MATERIALIZED column (never in SELECT *).
const demoDDL = `ts DateTime64(3) DEFAULT now64(3),
device_id UInt32,
seq UInt8 DEFAULT 0,
payload String,
grade Enum8('a' = 1, 'b' = 2),
payload_len UInt32 MATERIALIZED length(payload)`

// A small three-column table for the outcome and format sections. Positional
// formats (CSV, TSV, Values, RowBinary...) are far easier to read against a
// small schema, and both DEFAULTs give the empty-field rules something to do.
const formatDDL = `device_id UInt32, seq UInt8 DEFAULT 7, label String DEFAULT 'unknown'`

// Pinning the clock is what makes a demo with now64(3) in it reproducible.
// The value is a STRING at the boundary, always: 19 digits do not survive an
// IEEE double, and a JSON number here would be silently ignored.
const pinnedClock = "1700000000000000000" // 2023-11-14 22:13:20 UTC

// Hand-built binary payloads (hex), shared by all four tours. Each is
// explained where it is fed. The RBWD payloads are the committed fixtures
// from tests/fixtures/rbwd/, whose value bytes a real ClickHouse wrote.
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
	// raw column. 4 bytes ff ff ff ff — declared 4 wide, then (wrongly) 8 wide.
	buffersOK   = "010000000000000001000000000000000400000000000000ffffffff"
	buffersWide = "010000000000000001000000000000000800000000000000ffffffff"
)

// Section 11's CANNED server responses. These are not live bytes — they are
// shaped EXACTLY like a real ClickHouse's JSONEachRow answers to the three
// discovery queries (quoted UInt64s and all, matching a stock HTTP server's
// output_format_json_quote_64bit_integers=1). Swap in your own HTTP client's
// bytes and nothing else changes.
const (
	cannedVersionResult  = `{"version":"25.8.28.1"}` + "\n"
	cannedSettingsResult = `{"name":"flatten_nested","value":"0"}` + "\n" +
		`{"name":"date_time_input_format","value":"best_effort"}` + "\n"
	cannedColumnsResult = `{"name":"ts","type":"DateTime64(3)","default_kind":"DEFAULT","default_expression":"now64(3)","position":"1"}` + "\n" +
		`{"name":"device_id","type":"UInt32","default_kind":"","default_expression":"","position":"2"}` + "\n" +
		`{"name":"reading c","type":"Float64","default_kind":"","default_expression":"","position":"3"}` + "\n" +
		`{"name":"note","type":"String","default_kind":"DEFAULT","default_expression":"'unset'","position":"4"}` + "\n"
)

func main() {
	reg, lib := section1()
	section2(lib)
	section3(lib)
	section4(lib)
	section5(lib)
	section6(lib)
	section7(lib, reg)
	section8(lib)
	section9()
	section10(lib)
	section11(reg)
	section12(reg)
	section13()
	section14()
	section15(lib)
	section16(lib)

	blank()
	line("Done. Every value above was measured by this run.")
	line("The optional ONLINE demo (a real server, end to end) is go/ingest-demo/.")
}

// ---------------------------------------------------------------------------
// SECTION 1 — Load the library and check the ABI
//
// WHAT: open the artifact registry, see every ClickHouse version
// resident in this one process, pick one, and check the ABI revision.
// WHY: an ingest gateway serves tenants on different ClickHouse versions at
// once; the registry is how one process answers for all of them, exactly.
// LOOK FOR: the artifact NAMING ITSELF, and the refusal for a version that is
// not built — never a silent nearest-version fallback.
// C API: chs_clickhouse_version, chs_abi_revision, chs_init (implicit on
// load), chs_free (implicit on every returned string).
// ---------------------------------------------------------------------------
func section1() (*chtypes.Registry, *chtypes.Library) {
	section(1, "Load the library and check the ABI")

	dir := registryDir()
	reg, err := chtypes.NewRegistry(dir)
	if err != nil {
		fatal("open registry %q: %v\n\nFetch an artifact first: `scripts/fetch.sh 25.8`.", dir, err)
	}
	versions := reg.Versions()
	kv("registry dir", dir)
	kv("versions resident", strings.Join(versions, "  "))
	note("one dlopen (RTLD_LOCAL) per version — all live in THIS process at once")
	if len(versions) == 1 {
		note("only one artifact is built; the tour still runs, and section 12's")
		note("cross-version sweeps will degrade gracefully. More: `scripts/fetch.sh 26.7`")
	}

	// Version selection: a minor line ("25.8") and an exact patch
	// ("25.8.28.1-lts") both resolve. Docker tags drift, so an
	// exact-match-only lookup would silently lose a whole version line.
	want := os.Getenv("CHTYPES_VERSION")
	if want == "" {
		want = newestLine(versions)
		kv("version selected", want+"  (default: newest held; set $CHTYPES_VERSION to change)")
	} else {
		kv("version selected", want+"  (from $CHTYPES_VERSION)")
	}
	lib, err := reg.For(chtypes.Version(want))
	if err != nil {
		fatal("%v", err)
	}
	kv("artifact reports", string(lib.Version)+"  (minor line "+lib.Minor+")")
	note("the artifact names ITSELF via chs_clickhouse_version() — nothing is")
	note("ever inferred from a directory or file name")

	// The ABI revision closes the gap symbol presence cannot: a symbol proves
	// a function exists, never that its signature matches. The binding's
	// revision is a compile-time constant; the artifact's is a live call. A
	// nonzero disagreement is refused AT LOAD, not discovered mid-call.
	kv("ABI revision (binding)", strconv.Itoa(chtypes.ABIRevision))
	kv("ABI revision (artifact)", strconv.Itoa(lib.ABIRevision)+"   (0 would mean 'predates the probe')")
	kv("compile-settings symbol", fmt.Sprintf("%v  (Library.HasCompileSettings)", lib.HasCompileSettings()))

	// Graceful refusal: answering 26.7 semantics out of a 25.8 artifact would
	// be a lie, so an unknown version errors, NAMING what is loaded.
	blank()
	_, err = reg.For("99.9")
	kv("asking for 99.9", errStr(err))
	note("no nearest-neighbor fallback, ever — a wrong-version answer is a")
	note("wrong answer with a green checkmark on it")

	return reg, lib
}

// ---------------------------------------------------------------------------
// SECTION 2 — Ask a build about itself
//
// WHAT: type validation and canonicalization, straight from this build's own
// DataTypeFactory.
// WHY: canonicalization is how you compare a tenant's declared type against
// what the server will actually store — and it is NOT a spelling normalizer,
// it is the server's own parse.
// LOOK FOR: Variant members being SORTED, BIGINT becoming Int64, and the
// error for an unknown family carrying ClickHouse's own code 50.
// C API: chs_validate_type, plus the introspection trio — chs_reference_type,
// chs_registered_families, chs_function_flags — exposed per Library in every
// SDK since the 2026-08-26 parity cycle (docs/reference/bindings.md §Introspection).
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
	note("Variant members are SORTED; surplus parameters are dropped; the")
	note("space after each comma is the library's own spelling — compare")
	note("canonical strings verbatim, never re-normalize whitespace")
	blank()

	// An unknown family is a typed error carrying ClickHouse's OWN code and
	// message — not a string you have to pattern-match.
	_, err := lib.ValidateType("NotAType")
	var se *chtypes.SchemaError
	if errors.As(err, &se) {
		kv("ValidateType(NotAType)", fmt.Sprintf("*SchemaError code=%d  %s", se.Code, se.Msg))
		note("code 50 = UNKNOWN_TYPE — the server's own code, from the server's")
		note("own registry. Section 10 is the full error taxonomy.")
	}
	blank()

	// The introspection trio, per Library (docs/reference/bindings.md §Introspection).
	if ref, err := lib.ReferenceType("UInt8"); err == nil {
		kv("ReferenceType(UInt8)", "-> "+ref+"  (the widened second-parse type)")
	} else {
		kv("ReferenceType(UInt8)", errStr(err))
	}
	if families, err := lib.RegisteredFamilies(); err == nil {
		kv("RegisteredFamilies", fmt.Sprintf("%d type families (e.g. %s)", len(families), strings.Join(families[:3], ", ")))
	} else {
		kv("RegisteredFamilies", errStr(err))
	}
	if flags, err := lib.FunctionFlags(); err == nil {
		lines := 0
		for _, l := range strings.Split(flags, "\n") {
			if l != "" {
				lines++
			}
		}
		kv("FunctionFlags", fmt.Sprintf("%d registered functions audited (TSV)", lines))
		note("the volatility audit behind the statelessness gate — the build")
		note("fails unless the admitted volatile set is exactly the 4 clock reads")
	} else {
		kv("FunctionFlags", errStr(err))
	}
}

// ---------------------------------------------------------------------------
// SECTION 3 — Compile a schema and read it back
//
// WHAT: compile a column-declaration list (NOT a CREATE TABLE) and walk the
// compiled columns: canonical types, DEFAULT kinds and expressions.
// WHY: the compiled handle IS the table, as this ClickHouse version would
// create it. Two rewrites below are things no type-string comparison could
// ever catch — the compile is schema-aware.
// LOOK FOR: DEFAULT NULL turning Int64 into Nullable(Int64), and an ALIAS
// column whose type is INFERRED from its expression.
// C API: chs_schema_compile, chs_schema_column_count/_name/_type/
// _default_kind/_default_expr, chs_schema_free (via Close). The Go SDK does
// not surface chs_schema_column_default_is_literal (Python/TS/Rust do).
// ---------------------------------------------------------------------------
func section3(lib *chtypes.Library) {
	section(3, "Compile a schema and read it back")

	kv("the DDL", "")
	for _, l := range strings.Split(demoDDL, ",\n") {
		raw("      " + l)
	}
	s, err := lib.CompileDDL(demoDDL)
	if err != nil {
		fatal("%v", err)
	}
	blank()
	kv("compiled columns", "name  type  (default kind + expression)")
	for _, c := range s.Columns {
		extra := ""
		if c.DefaultKind != chtypes.KindNone {
			extra = "  " + c.DefaultKind.String() + " " + c.Default
		}
		kv("  "+c.Name, c.Type+extra)
	}
	note("payload_len is MATERIALIZED: compiled, introspectable, but never")
	note("read from input — watch it come back separately in section 6")
	s.Close() // chs_schema_free; freed handles refuse further calls
	blank()

	// Rewrite 1: a DEFAULT can change the declared TYPE. This is why
	// ValidateType alone is not enough and a schema-aware compile exists.
	s2, err := lib.CompileDDL("x Int64 DEFAULT NULL")
	if err != nil {
		fatal("%v", err)
	}
	kv("x Int64 DEFAULT NULL", "compiles as "+s2.Columns[0].Type+" DEFAULT "+s2.Columns[0].Default)
	note("the DEFAULT rewrote the type to Nullable — the server does this at")
	note("CREATE, so chtypes must too or every later verdict drifts")
	s2.Close()

	// Rewrite 2: an ALIAS column's type is inferred from its expression.
	s3, err := lib.CompileDDL("a UInt8, al ALIAS a + 1")
	if err != nil {
		fatal("%v", err)
	}
	kv("a UInt8, al ALIAS a + 1", "al compiles as "+s3.Columns[1].Type)
	note("UInt8 + 1 widens to UInt16, ClickHouse's own inference")
	s3.Close()
	blank()

	// A failed compile is the same typed error as section 2's.
	_, err = lib.CompileDDL("x NotAType")
	kv("x NotAType", classify(err))
	note(truncate(errStr(err), 90))
}

// ---------------------------------------------------------------------------
// SECTION 4 — Compile under a settings profile
//
// WHAT: the same compile with a DECLARED settings profile fixed into the
// handle — the settings a real server would have had at CREATE TABLE.
// WHY: some settings change the SHAPE of a table (flatten_nested), some gate
// which TYPES may exist (allow_suspicious_low_cardinality_types). A gateway
// discovers a deployment's settings once (section 11) and declares them here;
// the handle then behaves like a table created on THAT server.
// LOOK FOR: one DDL compiling to two different column lists; a type gate
// failing with the server's own 455; a typo'd setting name failing with the
// server's own 115 INCLUDING its did-you-mean hint.
// C API: chs_schema_compile (settings_json + mode arguments).
// ---------------------------------------------------------------------------
func section4(lib *chtypes.Library) {
	section(4, "Compile under a settings profile")

	// (a) A compile-SHAPE setting: the same DDL, two storage shapes.
	kv("(a) flatten_nested", "id UInt32, n Nested(a UInt8, b String)")
	for _, v := range []string{"1", "0"} {
		s, err := lib.CompileDDL("id UInt32, n Nested(a UInt8, b String)",
			chtypes.WithCompileSettings(map[string]string{"flatten_nested": v}))
		if err != nil {
			kv("  ="+v, errStr(err))
			continue
		}
		var names []string
		for _, c := range s.Columns {
			names = append(names, c.Name+" "+c.Type)
		}
		kv("  ="+v, strings.Join(names, " | "))
		s.Close()
	}
	note("under 1 (the stock default) the Nested column is stored FLATTENED as")
	note("two Arrays; under 0 it is one Array(Tuple) column. Every downstream")
	note("answer — names, arity, the RowBinary wire — follows the compiled shape.")
	blank()

	// (b) A TYPE GATE, declared: checked ONCE at compile with the server's own
	// code, exactly where a real server checks it (at CREATE).
	kv("(b) a type gate", "lc LowCardinality(UInt8), allow_suspicious_low_cardinality_types")
	for _, v := range []string{"0", "1"} {
		s, err := lib.CompileDDL("lc LowCardinality(UInt8)",
			chtypes.WithCompileSettings(map[string]string{"allow_suspicious_low_cardinality_types": v}))
		if err != nil {
			var se *chtypes.SchemaError
			code := "?"
			if errors.As(err, &se) {
				code = strconv.Itoa(se.Code)
			}
			kv("  ="+v, "REFUSED, the server's own code "+code)
			note(truncate(errStr(err), 92))
		} else {
			kv("  ="+v, "compiled: "+s.Columns[0].Type)
			s.Close()
		}
	}
	blank()

	// (c) A typo in the profile is caught at DECLARE time. The did-you-mean
	// hint is the SERVER'S — chtypes passes it through and invents nothing.
	_, err := lib.CompileDDL("a UInt8",
		chtypes.WithCompileSettings(map[string]string{"flatten_nestedd": "1"}))
	var se *chtypes.SchemaError
	if errors.As(err, &se) {
		kv("(c) unknown setting name", fmt.Sprintf("flatten_nestedd -> code %d (UNKNOWN_SETTING)", se.Code))
		note(truncate(se.Msg, 96))
	}
	blank()

	// (d) The compile MODE. One mode exists (DECLARED = 0). A binding passes
	// an unrecognized mode THROUGH; the refusal (-2, a decline) is the
	// library's to make — unconditionally, even with no profile.
	kv("(d) compile mode", "chtypes.CompileDeclared = "+strconv.Itoa(int(chtypes.CompileDeclared)))
	s, err := lib.CompileDDL("a UInt8", chtypes.WithCompileMode(chtypes.CompileDeclared))
	kv("  mode=0", okOr(err, "compiled"))
	if s != nil {
		s.Close()
	}
	_, err = lib.CompileDDL("a UInt8", chtypes.WithCompileMode(chtypes.CompileMode(7)))
	kv("  mode=7", classify(err))
	note("a DECLINE (-2), not a rejection: reserved for a future mode")
}

// ---------------------------------------------------------------------------
// SECTION 5 — Accept, reject, decline (and poison)
//
// WHAT: the three verdicts every row lands on — plus the fourth, poisoned,
// which looks like an accept and bites at read time.
// WHY: this is the contract of the whole product. An ingest gateway routes on
// exactly this: accepted -> insert and publish the STORED values; rejected ->
// 400 the producer with the server's own message; unsupported -> chtypes
// refuses to guess, so fall back to the real server (validate cautiously) and
// NEVER convert the decline into an accept or a reject yourself.
// LOOK FOR: the accept carrying a visible coercion (input 256, stored 0);
// the reject carrying ClickHouse's own error text; the decline carrying no
// ClickHouse code at all.
// C API: chs_rows.
// ---------------------------------------------------------------------------
func section5(lib *chtypes.Library) {
	section(5, "Accept, reject, decline (and poison)")
	kv("schema", formatDDL)
	blank()

	s, err := lib.CompileDDL(formatDDL)
	if err != nil {
		fatal("%v", err)
	}
	defer s.Close()

	// ACCEPT — with the coercion made visible. ClickHouse's readIntText wraps
	// integers mod 2^N and reports SUCCESS; chtypes derives the Transform so a
	// gateway can warn the tenant BEFORE the row ships to subscribers.
	kv("(a) ACCEPT", `{"device_id":1,"seq":256,"label":"ok"}`)
	feed(s, "JSONEachRow", chtypes.JSONEachRow, []byte(`{"device_id":1,"seq":256,"label":"ok"}`))
	note("accepted — but look at seq: input 256, stored 0. The ~ line is the")
	note("Transform (reason overflow_wrap, LOSSY). Publish the STORED value;")
	note("publishing the payload value is how previews and tables diverge.")
	blank()

	// REJECT — the server's own refusal, code and message verbatim from the
	// vendored ClickHouse code. Nothing to retry; tell the producer.
	kv("(b) REJECT", `{"device_id":"abc"}`)
	feed(s, "JSONEachRow", chtypes.JSONEachRow, []byte(`{"device_id":"abc"}`))
	note("code 27 and the message are ClickHouse's OWN — chtypes never")
	note("hand-writes an error, so your 400 body matches what a real INSERT")
	note("would have said")
	blank()

	// DECLINE — chtypes refuses to guess. Values falls back to the SQL
	// expression parser for non-literals; evaluating tenant SQL locally is a
	// guess this library will not make, so the row is UNSUPPORTED (-2).
	kv("(c) DECLINE", "(2,7,concat('a','b'))  as Values")
	feed(s, "Values", chtypes.Values, []byte(`(2,7,concat('a','b'))`))
	note("unsupported means 'a real server MIGHT WELL accept this; I will not")
	note("guess'. Do not 400 the producer (that manufactures an over-reject),")
	note("do not publish (that manufactures an over-accept): send it to the")
	note("real server unpreviewed and let it decide. Both mistake classes are")
	note("budgeted at zero in this repo.")
	blank()

	// POISON — the fourth verdict. The INSERT genuinely succeeds and every
	// later SELECT throws: RowBinaryWithDefaults' marker byte fills an Enum
	// with the raw zero, and Enum8('red'=1,'green'=2) has NO name for 0.
	kv("(d) POISON", "an accept that bites at read time")
	sp, err := lib.CompileDDL("id UInt32, e Enum8('red' = 1, 'green' = 2)")
	if err == nil {
		kv("  schema", "id UInt32, e Enum8('red' = 1, 'green' = 2)")
		kv("  payload (RBWD)", rbwdPoison+"   (id=1 by value, e by marker byte)")
		feed(sp, "RBWD marker", chtypes.RowBinaryWithDefaults, unhex(rbwdPoison))
		note("accepted_poisoned + code 691: the marker fills with the COLUMN-level")
		note("raw zero, and raw 0 has no Enum name. The insert returns success;")
		note("every later SELECT fails. Reported as an ACCEPT variant — never a")
		note("rejection — because the insert really does succeed.")
		kv("  control payload", rbwdValue+"   (e supplied by value = 2)")
		feed(sp, "RBWD value", chtypes.RowBinaryWithDefaults, unhex(rbwdValue))
		sp.Close()
	}
	blank()

	// SKIP — the fifth verdict (2026-08-27), and the only per-row-only one: a
	// batch under input_format_allow_errors_* drops a bad row and continues,
	// with the server's own machinery — and since the itemization cycle, every
	// skip keeps its place in Rows with the error IRowInputFormat caught
	// before resyncing. The server logs only a count; chtypes reports what it
	// computed.
	kv("(e) SKIP", "a bad middle row under input_format_allow_errors_num=10")
	batch := []byte(`{"device_id":1,"seq":1,"label":"a"}` + "\n" +
		`{"device_id":"oops"}` + "\n" +
		`{"device_id":3,"seq":3,"label":"c"}` + "\n")
	b, err := s.Rows(chtypes.JSONEachRow, batch,
		map[string]string{"input_format_allow_errors_num": "10"})
	if err != nil {
		fatal("%v", err)
	}
	kv("  batch", fmt.Sprintf("%s  rows_read=%d rows_skipped=%d",
		b.Outcome, b.RowsRead, b.RowsSkipped))
	for i, r := range b.Rows {
		line := r.Outcome.String()
		if r.Outcome == chtypes.Skipped {
			line += fmt.Sprintf("  code=%d %s", r.ErrCode, truncate(r.ErrMsg, 48))
		}
		kv(fmt.Sprintf("  row %d", i), line)
	}
	note("one Rows call answers per input record, IN ORDER: accepted (with")
	note("the coerced Values) or skipped (with the error that caused it).")
	note("A Skipped row is never stored — route on the row Outcome; forward")
	note("only survivors, and never send allow_errors to the real INSERT.")
}

// ---------------------------------------------------------------------------
// SECTION 6 — DEFAULT evaluation: where every value comes from
//
// WHAT: one row through the demo table with the clock pinned, then reading
// back WHERE each stored value came from (Value.Source), which values chtypes
// substituted itself, and which it computed.
// WHY: an INSERT is mostly values the row did NOT supply. A gateway that
// cannot answer "what will the table hold for this column?" cannot preview an
// insert. The volatile-DEFAULT rule is the sharp edge: chtypes resolved
// now64() from ITS clock, so the caller MUST send that column explicitly —
// otherwise the server stamps its own clock and preview != stored, always.
// LOOK FOR: four different Source values in one row; the Substituted warning;
// payload_len under Computed (never in Values); "bogus" under UnknownFields;
// and a skew-budget DECLINE at the end.
// C API: chs_row (via RowWithSettings), the chtypes_* clock settings.
// ---------------------------------------------------------------------------
func section6(lib *chtypes.Library) {
	section(6, "DEFAULT evaluation: where every value comes from")

	s, err := lib.CompileDDL(demoDDL)
	if err != nil {
		fatal("%v", err)
	}
	defer s.Close()

	row := []byte(`{"device_id":42,"seq":256,"payload":"hello","grade":"a","bogus":1}`)
	kv("row fed", string(row))
	kv("clock pinned", "chtypes_now_epoch_nanos="+pinnedClock+"  (2023-11-14 22:13:20 UTC)")
	note("ts and label are OMITTED on purpose; bogus matches no column")
	r, err := s.RowWithSettings(chtypes.JSONEachRow, row,
		map[string]string{"chtypes_now_epoch_nanos": pinnedClock})
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
	note("expression evaluated through ClickHouse's own CAST path),")
	note("default_substituted (a VOLATILE default resolved from the pinned")
	note("clock), absent (no DEFAULT: the type's own zero). Text is")
	note("ClickHouse's OWN JSON rendering — never re-serialized here, because")
	note("18446744073709551615 through a double comes back ...552000.")

	blank()
	kv("Substituted[]", "volatile DEFAULTs chtypes resolved from ITS clock")
	for _, sub := range r.Substituted {
		kv("  "+sub.Column, sub.Expr+"  ->  "+sub.Text)
	}
	note("SEND THESE AS EXPLICIT COLUMNS IN THE REAL INSERT. If the server")
	note("evaluates now64() itself, preview and stored differ every time —")
	note("ClickHouse reads the clock once per BLOCK, not once per statement.")

	blank()
	kv("Computed[]", "MATERIALIZED values — durable, but never in SELECT *")
	for _, c := range r.Computed {
		kv("  "+c.Column, c.Kind+"  =  "+c.Text)
	}
	note("reported separately from Values so the preview matches what a")
	note("subscriber reading the table will actually see")

	blank()
	kv("Transformed[]", "every silent change, with a machine-readable reason")
	for _, t := range r.Transformed {
		kv("  "+t.Reason, fmt.Sprintf("%s: %s -> %s   lossy=%v", t.Column, or(t.Input, "(absent)"), t.Stored, t.Lossy()))
	}
	note("Lossy() is false for exactly four reasons (reformat, default_filled,")
	note("zero_filled, default_materialized) and true for everything else")

	blank()
	kv("UnknownFields[]", fmt.Sprintf("%v", r.UnknownFields))
	kv("UnsupportedSettings[]", fmt.Sprintf("%v", r.UnsupportedSettings))
	note("unknown fields are reported, not judged — whether to 400 on them is")
	note("gateway policy. A non-empty UnsupportedSettings promotes the row to")
	note("unsupported: a declined setting must never score as agreement.")
	blank()

	// A DEFAULT can read OTHER columns of the same row.
	s2, err := lib.CompileDDL("a UInt8, d UInt8 DEFAULT a + 1")
	if err == nil {
		kv("row-dependent DEFAULT", "a UInt8, d UInt8 DEFAULT a + 1   fed CSV `7,`")
		feed(s2, "CSV", chtypes.CSV, []byte("7,"))
		note("the bare empty CSV field takes the DEFAULT, and the DEFAULT reads")
		note("a=7 from the same row — d stores 8, exactly as the server computes it")
		s2.Close()
	}
	blank()

	// The clock-skew budget: the one place a DEFAULT becomes a DECLINE. Past
	// the budget the only safe answer is "unsupported" — a substituted
	// timestamp too far in the past under a TTL is silently deleted at merge
	// time, with no error at any point.
	r2, err := s.RowWithSettings(chtypes.JSONEachRow, []byte(`{"device_id":1,"seq":1,"payload":"x","grade":"b"}`),
		map[string]string{"chtypes_clock_offset_nanos": "5000000000", "chtypes_max_clock_skew_nanos": "1"})
	if err == nil {
		kv("skew budget decline", "offset=5s, budget=1ns")
		kv("  Outcome / ErrCode", fmt.Sprintf("%v / %d", r2.Outcome, r2.ErrCode))
		for _, v := range r2.Values {
			if v.Column == "ts" {
				kv("  ts.Source", v.Source)
			}
		}
		note("default_volatile_unresolved: chtypes refuses to substitute a")
		note("volatile DEFAULT when the measured clock offset exceeds the")
		note("caller's budget (chtypes_max_clock_skew_nanos)")
	}
}

// ---------------------------------------------------------------------------
// SECTION 7 — One schema, every format
//
// WHAT: the same three-column schema fed in all ten chs_format encodings —
// accept and reject for each text format, then the binary tier with
// hand-built bytes.
// WHY: format is not cosmetic. Each format has signature behaviors (CSV's
// bare-vs-quoted empty field, RBWD's marker byte, Native's silent CAST,
// Buffers' silent reinterpret) that change what the table ends up holding.
// LOOK FOR: the same logical row giving format-specific verdicts, and the
// byte-level payloads in the comments — every binary payload is explained.
// C API: chs_rows with format codes 0..9 (frozen integers: JSONEachRow=0,
// CSV=1, TSV=2, Values=3, JSONCompactEachRow=4, RowBinary=5,
// RowBinaryWithDefaults=6, RowBinaryWithNamesAndTypesAndDefaults=7,
// Native=8, Buffers=9).
// ---------------------------------------------------------------------------
func section7(lib *chtypes.Library, reg *chtypes.Registry) {
	section(7, "One schema, every format")
	kv("schema", formatDDL)
	blank()

	s, err := lib.CompileDDL(formatDDL)
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
	note("DECLINE (section 5c) — evaluated by a real server, guessed by nobody")
	blank()

	// CSV's signature rule deserves its own two lines.
	kv("CSV empty-field rule", "bare empty takes the DEFAULT; quoted empty is ''")
	feed(s, "CSV   bare   2,7,", chtypes.CSV, []byte(`2,7,`))
	feed(s, `CSV   quoted 3,7,""`, chtypes.CSV, []byte(`3,7,""`))
	blank()

	group("binary formats (bytes, COUNTED — never NUL-terminated)")
	kv("  RowBinary payload", rowBinaryOK)
	feed(s, "RowBinary    accept", chtypes.RowBinary, unhex(rowBinaryOK))
	note("4-byte LE UInt32 (1), 1-byte UInt8 (7), varint-length String (\"ok\")")
	note("— no framing, no names, no self-description")
	feed(s, "RowBinary    reject", chtypes.RowBinary, []byte{0x01, 0x00})
	note("truncated mid-row: framing faults are all-or-nothing per batch")
	kv("  RBWD payload", rbwdMarker)
	feed(s, "RBWD  marker byte", chtypes.RowBinaryWithDefaults, unhex(rbwdMarker))
	note("RowBinaryWithDefaults prefixes each column with a marker byte:")
	note("00 = 'value follows', nonzero = 'compute the DEFAULT, read no value")
	note("bytes'. Above, seq's marker is 01 -> stored 7 (its DEFAULT).")
	kv("  RBWNTD payload", "(hand-built: LEB128 count, names, types, then a marker row)")
	feed(s, "RBWNTD       accept", chtypes.RowBinaryWithNamesAndTypesAndDefaults, unhex(rbwntdRow))
	note("format 7 arrives with ClickHouse 26.x — on an older artifact the line")
	note("above is the server's own 73 UNKNOWN_FORMAT, not a chtypes error.")
	note("Section 12 turns exactly this into the version-pinning lesson.")
	blank()

	group("Native: self-describing, and it CASTs")
	feed(s, "Native       accept", chtypes.Native, unhex(nativeOK))
	note("a Native block declares its OWN column names and types (captured")
	note("from a live 25.8 SELECT ... FORMAT Native)")
	feed(s, "Native       CAST", chtypes.Native, unhex(nativeCast))
	note("this block declares seq as String \"200\" while the table says UInt8:")
	note("the disagreement is CAST silently (input_format_native_allow_types_")
	note("conversion defaults to true on every vendored era) — visible here as")
	note("a Transform, invisible on a real server")
	blank()

	group("Buffers: NO self-description at all")
	newest, err := reg.For(chtypes.Version(newestLine(reg.Versions())))
	if err != nil {
		return
	}
	sn, err := newest.CompileDDL("x Int32")
	if err != nil {
		return
	}
	defer sn.Close()
	kv("  artifact", newest.Minor+"  (Buffers arrives at 26.5; this block uses the newest held)")
	kv("  payload", "4 bytes ff ff ff ff, declared x Int32")
	feed(sn, "Buffers reinterpret", chtypes.Buffers, unhex(buffersOK))
	note("a UInt32 producer's 4294967295 reads back as -1: same width, no")
	note("metadata, so no check CAN fire. The over-accept class in a format")
	note("that cannot detect it — the schema is entirely out of band.")
	feed(sn, "Buffers width", chtypes.Buffers, unhex(buffersWide))
	note("the same 4 bytes declared 8 wide IS caught: size accounting disagrees")
}

// ---------------------------------------------------------------------------
// SECTION 8 — Engines, MergeTree settings, and TTL
//
// WHAT: declare the table's engine and TTL, then watch the STORAGE layer
// change what a batch stores — including storing nothing at all.
// WHY: a row can be accepted per row and absent per batch. SummingMergeTree
// folds rows at insert; a TTL already in the past deletes them at merge, with
// no error at any point. A gateway reading only per-row verdicts previews
// rows the table will never hold.
// LOOK FOR: EngineRows (the stored truth) being SHORTER than the input; the
// TTL batch whose row is accepted and whose EngineRows is empty; and the
// refusal-vs-decline pair on MergeTree settings (the sign of the ABI return
// decides which).
// C API: chs_schema_engine, chs_schema_ttl, chs_rows.
// ---------------------------------------------------------------------------
func section8(lib *chtypes.Library) {
	section(8, "Engines, MergeTree settings, and TTL")

	// (a) A specialized engine changes what the table STORES.
	s, err := lib.CompileDDL("day Date, key UInt32, v UInt64")
	if err != nil {
		fatal("%v", err)
	}
	kv("(a) SetEngine", "SummingMergeTree ORDER BY (day, key)")
	if err := s.SetEngine("SummingMergeTree", "(day, key)"); err != nil {
		kv("  failed", errStr(err))
		s.Close()
		return
	}
	body := []byte(`{"day":"2026-01-01","key":1,"v":5}` + "\n" + `{"day":"2026-01-01","key":1,"v":7}`)
	b, err := s.Rows(chtypes.JSONEachRow, body, nil)
	if err != nil {
		fatal("%v", err)
	}
	kv("  rows in / rows_read", fmt.Sprintf("2 / %d   (v=5 and v=7, same key)", b.RowsRead))
	kv("  EngineRows (stored)", fmt.Sprintf("%s", b.EngineRows))
	note("two rows in, ONE row out, v summed — EngineRows is the post-merge")
	note("preview and, when present, the truth to believe over Rows")
	s.Close()
	blank()

	// (b) MergeTree-namespace settings: two failures, two KINDS. The sign of
	// the ABI return decides — a positive code is the SERVER refusing, a
	// negative one is this LIBRARY declining. Never flatten them.
	kv("(b) MergeTree settings", "refusal vs decline vs inert")
	s2, _ := lib.CompileDDL("a UInt8")
	err = s2.SetEngine("MergeTree", "tuple()",
		chtypes.WithMergeTreeSettings(map[string]string{"index_granularityy": "8192"}))
	kv("  unknown NAME", "index_granularityy -> "+classify(err))
	note(truncate(errStr(err), 90))
	note("the server's own 115: this DDL can never exist — tell the tenant")
	s2.Close()
	s3, _ := lib.CompileDDL("a UInt8")
	err = s3.SetEngine("MergeTree", "tuple()",
		chtypes.WithMergeTreeSettings(map[string]string{"index_granularity": "4096"}))
	kv("  known, non-default", "index_granularity=4096 -> "+classify(err))
	note("a DECLINE: no MergeTree setting's behavior is modeled yet, and")
	note("silently ignoring a declared value would fake the profile being in")
	note("force. A real server might well accept it — validate cautiously.")
	s3.Close()
	s4, _ := lib.CompileDDL("a UInt8")
	err = s4.SetEngine("MergeTree", "tuple()",
		chtypes.WithMergeTreeSettings(map[string]string{"index_granularity": "8192"}))
	kv("  known, AT default", "index_granularity=8192 -> "+okOr(err, "accepted (inert)"))
	s4.Close()
	blank()

	// (c) TTL: accepted per row, gone per batch.
	s5, err := lib.CompileDDL("ts DateTime, v UInt8")
	if err != nil {
		fatal("%v", err)
	}
	defer s5.Close()
	kv("(c) SetTTL", okOr(s5.SetEngine("MergeTree", "ts"), "MergeTree ORDER BY ts")+", TTL "+okOr(s5.SetTTL("ts + INTERVAL 1 DAY"), "ts + INTERVAL 1 DAY"))
	bt, err := s5.Rows(chtypes.JSONEachRow, []byte(`{"ts":"2020-01-01 00:00:00","v":9}`),
		map[string]string{"chtypes_now_epoch_nanos": pinnedClock})
	if err != nil {
		fatal("%v", err)
	}
	kv("  row fed", `{"ts":"2020-01-01 00:00:00","v":9}  with the clock pinned to 2023`)
	kv("  row-level outcome", bt.Rows[0].Outcome.String()+"   <- the row PARSED fine")
	kv("  EngineRows (stored)", fmt.Sprintf("%v  (length %d)", bt.EngineRows, len(bt.EngineRows)))
	for _, t := range bt.Transformed {
		kv("  batch transform", fmt.Sprintf("row=%d column=%q reason=%s lossy=%v", t.Row, t.Column, t.Reason, t.Lossy()))
	}
	note("accepted per ROW, stored nowhere per BATCH: the 2020 timestamp is")
	note("already past the TTL, so the part holds nothing. On a real server")
	note("this is a silent merge-time delete — the ttl_expired transform is")
	note("the only warning anyone gets.")
	blank()

	// (d) A TTL this library will not guess at.
	err = s5.SetTTL("now() + INTERVAL 1 DAY")
	kv("(d) SetTTL now()+1 DAY", classify(err))
	note(truncate(errStr(err), 90))
	note("a clock-reading TTL is DECLINED, not guessed")
}

// ---------------------------------------------------------------------------
// SECTION 9 — Settings precedence: who wins
//
// WHAT: the same row and the same handle, answered differently as settings
// are supplied at each layer:
//
//	per-call  >  handle profile  >  library defaults  >  ClickHouse's own
//
// WHY: this is how a gateway declares a deployment's settings ONCE (at
// compile) yet still lets one INSERT override per call. If precedence were
// fuzzy, the declared profile would not actually be in force.
// LOOK FOR: layers 3 vs 4 — the SAME handle, the SAME bytes, passing under
// the handle profile and failing the moment a per-call value overrides it.
// Then the library-defaults layer measured via SetDefaultSettings, including
// a WHOLESALE refusal that commits nothing.
// C API: chs_rows (settings_json), chs_set_default_settings.
// ---------------------------------------------------------------------------
func section9() {
	section(9, "Settings precedence: who wins")
	kv("the probe", `ts DateTime  fed  {"ts":"2026-01-15T10:30:00Z"}`)
	note("stock ClickHouse parses 'basic' datetimes only; best_effort accepts")
	note("ISO-8601 — so the verdict TELLS you which setting value won")
	note("(this section runs on Go's static path — see the comment for why)")
	blank()

	// Go note: on the dlopen'd Registry path, SetDefaultSettings is
	// STRUCTURALLY unreachable — the function-pointer table deliberately
	// omits chs_set_default_settings, because it reallocates a process-global
	// the row path reads by reference (see docs/reference/bindings.md §Teardown). The
	// static cgo path (one artifact, linked at build time — here lib/build)
	// exposes it as the package-level SetDefaultSettings, so this section
	// demonstrates all four layers there. Python/TS/Rust expose the seed on
	// their dlopen'd Library and their section 9 uses the selected version.
	iso := []byte(`{"ts":"2026-01-15T10:30:00Z"}`)
	basic := map[string]string{"date_time_input_format": "basic"}
	bestEffort := map[string]string{"date_time_input_format": "best_effort"}
	section9Static(iso, basic, bestEffort)
}

// ---------------------------------------------------------------------------
// SECTION 10 — The error taxonomy
//
// WHAT: every kind of answer this SDK gives, told apart BY TYPE — never by
// string matching.
// WHY: the three kinds demand three different reactions (tell the tenant /
// fall back cautiously / fix the deployment), and Go's two error types are
// deliberately PEERS: a decline can never satisfy errors.As against the
// refusal type, so a caller handling only refusals cannot silently convert
// declines into rejections.
// LOOK FOR: the same errors.As switch you would write in production, and the
// reminder that ROW verdicts are data (RowResult.Outcome), not errors.
// ---------------------------------------------------------------------------
func section10(lib *chtypes.Library) {
	section(10, "The error taxonomy")

	kv("the Go idiom", "errors.As against two PEER types")
	raw("      var ue *chtypes.UnsupportedError   // a DECLINE — validate cautiously")
	raw("      var se *chtypes.SchemaError        // a REFUSAL — se.Code is ClickHouse's")
	raw("      switch {")
	raw("      case errors.As(err, &ue): ...")
	raw("      case errors.As(err, &se): ...")
	raw("      }")
	blank()

	// A REFUSAL: ClickHouse's own code rides on *SchemaError.
	_, err := lib.CompileDDL("x NotAType")
	describeError(err, "compile x NotAType")

	// A DECLINE: *UnsupportedError, no ClickHouse code to carry.
	s, _ := lib.CompileDDL("ts DateTime, v UInt8")
	_ = s.SetEngine("MergeTree", "ts")
	err = s.SetTTL("now() + INTERVAL 1 DAY")
	describeError(err, "SetTTL now()+1 DAY")
	s.Close()

	// A registry miss is a plain error naming what IS loaded (Go has no
	// dedicated registry error type; Python/TS have RegistryError, Rust has
	// dedicated Error variants).
	blank()
	kv("registry miss", "a plain error that names what IS loaded")
	note("(see section 1's For(\"99.9\") — same message)")
	blank()

	kv("row verdicts are DATA", "RowResult.Outcome, not an error return")
	note("a row the server would reject comes back (RowResult, nil): the")
	note("Result is about whether the question could be asked; the verdict —")
	note("accepted / rejected / accepted_poisoned / unsupported — lives in the")
	note("answer. The Go error return fires only for a closed schema or an")
	note("unreadable result document.")
	kv("CodeUnsupported", strconv.Itoa(chtypes.CodeUnsupported)+"  (the wire sentinel; never a real ClickHouse code)")
}

// ---------------------------------------------------------------------------
// SECTION 11 — The discovery kit, offline
//
// WHAT: the three canonical queries chtypes ships for learning who a
// deployment is, their typed parsers, and ReconstructDDL — run here against
// CANNED bytes shaped exactly like a real server's JSONEachRow responses.
// WHY: chtypes NEVER opens a socket. You run these queries with whatever
// client you already have; the kit gives you the SQL and parses the results.
// The payoff is the last step: the server's own version string resolves an
// artifact, and the discovered settings become the compile profile — so the
// handle behaves like a table created on THAT deployment.
// LOOK FOR: the reconstructed DDL (backticks where needed, DEFAULTs carried),
// and the SAME ROW accepted under the discovered profile but rejected under a
// stock compile — the measurable reason discovery matters.
// C API: none until the compile at the end — the kit is pure client-side.
// (The ONLINE version of this flow, against a real server, is
// go/ingest-demo/ — the optional demo chplay.sh never runs.)
// ---------------------------------------------------------------------------
func section11(reg *chtypes.Registry) {
	section(11, "The discovery kit, offline")
	kv("NOTE", "responses below are CANNED — shaped exactly like a real")
	kv("", "server's, so the parsers cannot tell. Swap in your HTTP client.")
	blank()

	// Query 1: who are you? (version)
	kv("QueryServerVersion", chtypes.QueryServerVersion)
	kv("  canned response", strings.TrimSpace(cannedVersionResult))
	version, err := chtypes.ParseVersionResult([]byte(cannedVersionResult))
	if err != nil {
		fatal("%v", err)
	}
	kv("  parsed", version)
	blank()

	// Query 2: which settings did this deployment change from stock?
	kv("QueryChangedSettings", chtypes.QueryChangedSettings)
	for _, l := range strings.Split(strings.TrimSpace(cannedSettingsResult), "\n") {
		kv("  canned response", l)
	}
	settings, err := chtypes.ParseChangedSettingsResult([]byte(cannedSettingsResult))
	if err != nil {
		fatal("%v", err)
	}
	kv("  parsed", fmt.Sprintf("%d changed settings -> the compile profile", len(settings)))
	blank()

	// Query 3: what does the table look like AS STORED?
	kv("QueryTableColumns", "(system.columns for one table; see the constant)")
	cols, err := chtypes.ParseColumnsResult([]byte(cannedColumnsResult))
	if err != nil {
		fatal("%v", err)
	}
	for _, c := range cols {
		kind := ""
		if c.DefaultKind != "" {
			kind = "  " + c.DefaultKind + " " + c.DefaultExpression
		}
		kv(fmt.Sprintf("  [%d] %s", c.Position, c.Name), c.Type+kind)
	}
	note("default_kind/default_expression are CARRIED — dropping them would")
	note("silently lose the DEFAULT semantics sections 6 and 8 run on")
	ddl, err := chtypes.ReconstructDDL(cols)
	if err != nil {
		fatal("%v", err)
	}
	kv("  ReconstructDDL", ddl)
	note("`reading c` came back BACKTICKED — identifiers are quoted exactly")
	note("where ClickHouse requires it")
	blank()

	// The payoff: version -> artifact, settings -> profile, and a measurable
	// difference the discovered profile makes.
	lib, err := reg.For(chtypes.Version(version))
	if err != nil {
		kv("registry.For("+version+")", errStr(err))
		note("no artifact for this line — `scripts/fetch.sh 25.8` would add it; the")
		note("rest of this section needs it and is skipped")
		return
	}
	kv("registry.For("+version+")", "artifact "+string(lib.Version)+"  (exact patch -> the "+lib.Minor+" line)")
	sProf, err := lib.CompileDDL(ddl, chtypes.WithCompileSettings(settings))
	if err != nil {
		fatal("%v", err)
	}
	defer sProf.Close()
	sPlain, err := lib.CompileDDL(ddl)
	if err != nil {
		fatal("%v", err)
	}
	defer sPlain.Close()
	row := []byte(`{"ts":"2026-01-15T10:30:00Z","device_id":9,"reading c":21.5}`)
	kv("the same row, twice", string(row))
	bProf, _ := sProf.Rows(chtypes.JSONEachRow, row, nil)
	kv("  under the discovered profile", verdict(bProf))
	bPlain, _ := sPlain.Rows(chtypes.JSONEachRow, row, nil)
	kv("  under a stock compile", verdict(bPlain))
	note("the deployment declared date_time_input_format=best_effort, so ITS")
	note("server takes the ISO-8601 timestamp — a stock compile answers for a")
	note("server the tenant does not have. Discovery is what closes that gap.")
}

// ---------------------------------------------------------------------------
// SECTION 12 — Version pinning: same input, different answers
//
// WHAT: the same DDL and the same bytes, swept across every artifact resident
// in this process.
// WHY: version differences are the reason the registry exists. They are not
// monotonic — newer is NOT always more permissive — so no rule can predict
// them; only the real per-version artifact can answer.
// LOOK FOR: 25.10 rejecting a DEFAULT that both 25.8 and 26.5 accept; and the
// Buffers format simply not existing before 26.5 (the server's own 73).
// ---------------------------------------------------------------------------
func section12(reg *chtypes.Registry) {
	section(12, "Version pinning: same input, different answers")
	versions := reg.Versions()
	if len(versions) < 2 {
		kv("versions resident", strings.Join(versions, "  "))
		note("only one artifact is built, so there is nothing to sweep — the")
		note("point of this section needs at least two. Build another line")
		note("(e.g. `scripts/fetch.sh 26.7`) and re-run to see the answers diverge.")
		return
	}

	kv("(a) a mixed-type DEFAULT", "a UInt8, x Int64 DEFAULT if(1,2,'a')")
	for _, v := range versions {
		lib, err := reg.For(chtypes.Version(v))
		if err != nil {
			continue
		}
		s, err := lib.CompileDDL("a UInt8, x Int64 DEFAULT if(1,2,'a')")
		if err != nil {
			var se *chtypes.SchemaError
			if errors.As(err, &se) {
				kv("  "+v, fmt.Sprintf("REJECTED code %d  %s", se.Code, truncate(se.Msg, 52)))
			} else {
				kv("  "+v, errStr(err))
			}
			continue
		}
		kv("  "+v, "compiled  ("+s.Columns[1].Type+" DEFAULT "+s.Columns[1].Default+")")
		s.Close()
	}
	note("NEWER IS NOT ALWAYS MORE PERMISSIVE — no monotonic rule predicts")
	note("this, which is exactly why one real artifact per line exists")
	blank()

	kv("(b) a format's arrival", "Buffers (code 9), added in ClickHouse 26.5")
	kv("  payload", "1 column, 1 row, 4 bytes ff ff ff ff, declared x Int32")
	for _, v := range versions {
		lib, err := reg.For(chtypes.Version(v))
		if err != nil {
			continue
		}
		s, err := lib.CompileDDL("x Int32")
		if err != nil {
			continue
		}
		b, err := s.Rows(chtypes.Buffers, unhex(buffersOK), nil)
		switch {
		case err != nil:
			kv("  "+v, "call failed: "+err.Error())
		case b.Outcome == chtypes.Accepted && len(b.Rows) > 0:
			kv("  "+v, "accepted  x = "+b.Rows[0].Values[0].Text)
		default:
			kv("  "+v, fmt.Sprintf("%v  code=%d  %s", b.Outcome, b.ErrCode, truncate(b.ErrMsg, 40)))
		}
		s.Close()
	}
	note("73 UNKNOWN_FORMAT is the SERVER'S own answer on the older lines —")
	note("probe the artifact (one payload through Rows) instead of trusting")
	note("your own version arithmetic")
}

// ---------------------------------------------------------------------------
// SECTION 13 — Teardown
//
// WHAT: what to release, and when.
// WHY: schema handles are C allocations (chs_schema_free via Close). Process
// teardown is chs_shutdown — and Go's Registry deliberately exposes NO
// close/shutdown, which is a considered design, not an omission.
// C API: chs_schema_free, chs_shutdown.
// ---------------------------------------------------------------------------
func section13() {
	section(13, "Teardown")
	kv("schema handles", "Close() each compiled schema when done (chs_schema_free)")
	kv("Registry teardown", "none — deliberately")
	note("Go never dlcloses an artifact, and chs_init registers chs_shutdown")
	note("with atexit(), so an ordinary process needs no call. Omitting the")
	note("symbol from the function-pointer table is also what makes")
	note("chs_set_default_settings structurally unreachable from a Library —")
	note("a property worth more than the entry point (docs/reference/bindings.md")
	note("§Teardown). Python (registry.close()), TS (close()/Symbol.dispose)")
	note("and Rust (Registry::shutdown()) each show their SDK's shape.")
}

// ---------------------------------------------------------------------------
// SECTION 14 — The static path (Go only)
//
// WHAT: the second, cgo-linked shape only Go has — ONE artifact (lib/build,
// whatever version it was built from) linked at build time, no dlopen, used
// through package-level functions.
// WHY: it is the fast path for a binary that serves exactly one ClickHouse
// version, and it is the only place Go exposes SetDefaultSettings (section 9)
// and RegisteredFamilies. The Registry is the product path; docs/reference/bindings.md
// makes this static shape explicitly optional, and the other three SDKs
// (ctypes/ffi-rs/libloading — always dlopen) print a stub for this section.
// C API: same 22 functions, resolved by the linker instead of dlsym.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// SECTION 15 — Export: bytes + spans
//
// WHAT: the SAME chs_rows call that judges a batch can also serialize its
// accepted rows to wire bytes (JSONCompactEachRow this revision), addressed
// per row by index-aligned spans — RowsExport, one C call, never a second.
// WHY: every consumer of an accepted row wants the stored bytes ready to
// publish or INSERT without rebuilding them from the document — reassembly
// is where caller bugs live (the invalid-JSON-on-poisoned-rows class).
// LOOK FOR: the skipped row's {0,0} span; a span slice BEING the row's line;
// the poisoned batch DECLINING the export and saying why (fail-closed: an
// unreadable value cannot be honestly serialized); emitted-EMPTY (an answer)
// vs declined (not one); and the lean document (doc flags 0) keeping every
// verdict while dropping the description.
// C API: chs_rows with export_format/doc_flags/out_bytes (ABI revision 3).
// ---------------------------------------------------------------------------
func section15(lib *chtypes.Library) {
	section(15, "Export: bytes + spans")

	cs, err := lib.CompileDDL(formatDDL)
	must(err)
	defer cs.Close()

	body := []byte(`{"device_id":1,"label":"ok"}` + "\n" +
		`{"device_id":"zap"}` + "\n" +
		`{"device_id":3,"label":"hi"}` + "\n")
	res, err := cs.RowsExport(chtypes.JSONEachRow, body,
		map[string]string{"input_format_allow_errors_num": "10"},
		chtypes.JSONCompactEachRow, chtypes.DocAll)
	var ue *chtypes.UnsupportedError
	if errors.As(err, &ue) {
		note("this artifact predates the export surface (relink it to ABI")
		note("revision 3, `just refresh <version>`); the section degrades here")
		note("rather than failing the tour — the surface is additive.")
		return
	}
	must(err)
	if res.Outcome == chtypes.Unsupported {
		kv("RowsExport", "declined: "+truncate(res.ErrMsg, 64))
		note("this artifact answers the export request unsupported; relink the")
		note("fleet (wave 3 / `just refresh`) to see the live bytes. Degrading.")
		return
	}
	kv("batch", fmt.Sprintf("%s  rows_read=%d rows_skipped=%d", res.Outcome, res.RowsRead, res.RowsSkipped))
	kv("payload", fmt.Sprintf("%q  (%d bytes, one line per ACCEPTED row)", res.Payload, len(res.Payload)))
	note("wire order = declared minus MATERIALIZED/ALIAS/EPHEMERAL, so these")
	note("bytes are directly INSERT-able with no column list; DEFAULTs (seq=7,")
	note("label='unknown') are already applied — preview == stored")
	for i, sp := range res.Spans {
		lineOf := "(no bytes — row not accepted)"
		if sp.Len > 0 {
			lineOf = fmt.Sprintf("%q", res.Payload[sp.Off:sp.Off+sp.Len])
		}
		kv(fmt.Sprintf("  span[%d] {off:%d len:%d}", i, sp.Off, sp.Len),
			fmt.Sprintf("%s -> %s", res.Rows[i].Outcome, lineOf))
	}
	note("spans are INDEX-ALIGNED with rows; slicing spans out of the payload")
	note("IS the per-row payload, and concatenating non-zero spans reproduces")
	note("it exactly — batches merge by byte concatenation")
	blank()

	// The lean document: doc flags 0 (RowsExport's default) keeps the whole
	// verdict channel and drops the description — same bytes, thinner JSON.
	lean, err := cs.RowsExport(chtypes.JSONEachRow, body,
		map[string]string{"input_format_allow_errors_num": "10"},
		chtypes.JSONCompactEachRow)
	must(err)
	kv("lean (flags 0)", fmt.Sprintf("outcome %s, %d verdict rows, %d Values, %d Transformed — payload identical: %v",
		lean.Outcome, len(lean.Rows), len(lean.Rows[0].Values), len(lean.Transformed),
		string(lean.Payload) == string(res.Payload)))
	note("flags thin the DESCRIPTION, never the VERDICT; DocValues /")
	note("DocTransforms / DocDefaults pick groups à la carte")
	blank()

	// Fail-closed: a poisoned batch holds a value ClickHouse itself cannot
	// read back — no writer can honestly serialize it, so no bytes.
	ps, err := lib.CompileDDL(`e Enum8('a' = 1, 'b' = 2)`)
	must(err)
	defer ps.Close()
	poi, err := ps.RowsExport(chtypes.JSONEachRow, []byte(`{"e":null}`+"\n"),
		map[string]string{"input_format_defaults_for_omitted_fields": "0"},
		chtypes.JSONCompactEachRow, chtypes.DocAll)
	must(err)
	pv := "nil (declined)"
	if poi.Payload != nil {
		pv = fmt.Sprintf("%d bytes", len(poi.Payload))
	}
	kv("poisoned batch", fmt.Sprintf("%s -> payload=%s export_declined=%q",
		poi.Outcome, pv, truncate(poi.ExportDeclined, 48)))

	// Emitted-empty is an ANSWER (zero accepted rows), not a decline.
	emp, err := cs.RowsExport(chtypes.JSONEachRow, nil, nil, chtypes.JSONCompactEachRow)
	must(err)
	kv("empty batch", fmt.Sprintf("payload non-nil=%v len=%d  (emitted-empty != declined)",
		emp.Payload != nil, len(emp.Payload)))
}

// ---------------------------------------------------------------------------
// SECTION 16 — Filters: WHERE semantics at the edge
//
// WHAT: compile one boolean expression against a schema (CompileFilter) and
// evaluate it per row of a body — ClickHouse's own comparison functions, so
// the answers are WHERE-side by construction.
// WHY: read-side row visibility (who may SEE this row) is a WHERE question,
// and WHERE coercion is NOT insert coercion: `x = 256` over UInt8 PROMOTES
// (false for every row) where an insert would wrap 256 to 0. Reusing the
// insert answer would silently match every legitimate zero.
// LOOK FOR: 'f','f' where the insert path stores 0; NULL being not-true; the
// 'e' class (compiles, then THROWS per row — a server fails the WHOLE query
// here); clock reads and {p:Type} parameters REFUSED at compile, never
// guessed; and the enforcement gate at the end.
// C API: chs_filter_compile / chs_filter_rows / chs_filter_free (revision 3).
// ---------------------------------------------------------------------------
func section16(lib *chtypes.Library) {
	section(16, "Filters: WHERE semantics at the edge")

	cs, err := lib.CompileDDL("x UInt8")
	must(err)
	defer cs.Close()

	f, err := cs.CompileFilter("x = 256")
	var ue *chtypes.UnsupportedError
	if errors.As(err, &ue) {
		note("this artifact predates the filter surface (relink it to ABI")
		note("revision 3, `just refresh <version>`); the section degrades here")
		note("rather than failing the tour — the surface is additive.")
		return
	}
	must(err)
	fr, err := f.Rows(chtypes.JSONEachRow, []byte(`{"x":0}`+"\n"+`{"x":255}`+"\n"), nil)
	must(err)
	kv("filter `x = 256` over UInt8", "verdicts "+verdictString(fr))
	note("PROMOTES, never wraps: false for x=0 AND x=255. The insert side of")
	note("this same library stores 256 as 0 (section 5's overflow_wrap) —")
	note("which is why predicate constants must never be folded through")
	note("insert coercion (docs/reference/bindings.md §Constants are not payloads)")
	f.Close()
	blank()

	// NULL is not true — three-valued logic collapsed at the WHERE boundary.
	ns, err := lib.CompileDDL("lvl Nullable(UInt8)")
	must(err)
	defer ns.Close()
	nf, err := ns.CompileFilter("lvl = 1")
	must(err)
	fr, err = nf.Rows(chtypes.JSONEachRow, []byte(`{"lvl":null}`+"\n"+`{"lvl":1}`+"\n"), nil)
	must(err)
	kv("`lvl = 1` on [null, 1]", "verdicts "+verdictString(fr)+"   (NULL is not true, as WHERE hides it)")
	nf.Close()
	blank()

	// The 'e' class: compiles clean, then THROWS on every row's values.
	ss, err := lib.CompileDDL("s String")
	must(err)
	defer ss.Close()
	sf, err := ss.CompileFilter("s = 257")
	must(err)
	fr, err = sf.Rows(chtypes.JSONEachRow, []byte(`{"s":"hi"}`+"\n"), nil)
	must(err)
	kv("`s = 257` over String", "verdicts "+verdictString(fr))
	for _, fe := range fr.Errors {
		kv(fmt.Sprintf("  row %d", fe.Row), fmt.Sprintf("code %d  %s", fe.Code, truncate(fe.Msg, 56)))
	}
	note("on a real server this WHERE fails the WHOLE query — 'e' is NOT an")
	note("answer, and neither is 'd' (a row this library declines): an")
	note("enforcing caller fails CLOSED on both, or NOT(decline-as-false)")
	note("inverts fail-closed into fail-open — the measured leak class")
	sf.Close()
	blank()

	// A bad row declines ('d'), itemized, and the tail keeps its indexes.
	df, err := cs.CompileFilter("x < 5")
	must(err)
	fr, err = df.Rows(chtypes.JSONEachRow,
		[]byte(`{"x":1}`+"\n"+`{"x":"zap"}`+"\n"+`{"x":9}`+"\n"), nil)
	must(err)
	kv("`x < 5` on [1, bad, 9]", "verdicts "+verdictString(fr)+"   (the bad row cannot swallow the tail)")
	df.Close()
	blank()

	// Refused at compile, never guessed — and the two REASONS are two TYPES.
	_, err = cs.CompileFilter("now() > x")
	kv("compile `now() > x`", classify(err))
	_, err = cs.CompileFilter("x = {p:UInt8}")
	kv("compile `x = {p:UInt8}`", classify(err))
	note("clock reads would be answered with THIS process's clock, not the")
	note("server's; an UNBOUND {p:Type} is the SERVER's own 456 since ABI")
	note("revision 4 (\"Substitution `p` is not set\") — bind it instead")
	_, err = cs.CompileFilter("nosuch = 1")
	kv("compile `nosuch = 1`", classify(err))
	blank()

	// -- revision 4 sub-demo: query parameters ---------------------------
	// One 3-row "event" body, shared with the twin sub-demo below.
	group("query parameters (ABI revision 4) — values are STRINGS, never escaped")
	ps, err := lib.CompileDDL("tenant String, role String, x UInt8")
	must(err)
	defer ps.Close()
	eventBody := []byte(`{"tenant":"acme","role":"admin","x":1}` + "\n" +
		`{"tenant":"evil","role":"viewer","x":2}` + "\n" +
		`{"tenant":"' OR 1=1 --","role":"admin","x":3}` + "\n")

	tf, err := ps.CompileFilter("tenant = {t:String}",
		chtypes.WithFilterParams(map[string]string{"t": "acme"}))
	var pue *chtypes.UnsupportedError
	if errors.As(err, &pue) {
		note("this artifact predates the params surface (relink it to ABI")
		note("revision 4, `just refresh <version>`); the sub-demo degrades")
		note("here rather than failing the tour — the surface is additive.")
	} else {
		must(err)
		fr, err := tf.Rows(chtypes.JSONEachRow, eventBody, nil)
		must(err)
		kv("`tenant = {t:String}`, t=acme", "verdicts "+verdictString(fr))
		tf.Close()
		note("compiled ONCE per (schema, expr, params) — the value is baked in;")
		note("a per-tenant cache MUST be a bounded LRU + a compile throttle")

		hostile := `' OR 1=1 --`
		hf, err := ps.CompileFilter("tenant = {t:String}",
			chtypes.WithFilterParams(map[string]string{"t": hostile}))
		must(err)
		fr, err = hf.Rows(chtypes.JSONEachRow, eventBody, nil)
		must(err)
		hostileOK := fr.Outcome == chtypes.FilterOK && len(fr.Verdicts) == 3 &&
			fr.Verdicts[0] == chtypes.VerdictFalse && fr.Verdicts[1] == chtypes.VerdictFalse &&
			fr.Verdicts[2] == chtypes.VerdictTrue
		kv("t = `' OR 1=1 --` (hostile)", fmt.Sprintf("verdicts %s   hostile-value-inert: %v", verdictString(fr), hostileOK))
		hf.Close()
		note("the value became a typed LITERAL after SQL parsing — it matches")
		note("only the row holding exactly that string; no OR 1=1 semantics,")
		note("and NOTHING was escaped to get there (never hand-escape values)")
		note("traps: size the {brace type} for the value's domain ({p:UInt8}")
		note("given \"256\" BINDS 0 — the reader wraps); and never NAME a param")
		note("`limit`/`offset` — a real server's TCP channel refuses those")
	}
	blank()

	// -- revision 4 sub-demo: the block twin -----------------------------
	group("the block twin (ABI revision 4) — parse ONCE, evaluate K filters")
	blockHandle, err := ps.ParseBlock(chtypes.JSONEachRow, eventBody, nil)
	if errors.As(err, &pue) {
		note("this artifact predates the block twin (relink it to ABI")
		note("revision 4, `just refresh <version>`); the sub-demo degrades")
		note("here rather than failing the tour — the surface is additive.")
	} else {
		must(err)
		adminF, err := ps.CompileFilter("role = 'admin'")
		must(err)
		viewerF, err := ps.CompileFilter("role = 'viewer'")
		must(err)
		fr, err := adminF.Eval(blockHandle)
		must(err)
		kv("eval `role = 'admin'`", "verdicts "+verdictString(fr))
		fr, err = viewerF.Eval(blockHandle)
		must(err)
		kv("eval `role = 'viewer'`", "verdicts "+verdictString(fr))
		adminF.Close()
		viewerF.Close()
		blockHandle.Close()
		note("ONE parse of the 3-row event fed BOTH filters: eval neither")
		note("consumes nor mutates the block, and Eval(ParseBlock(body)) ≡")
		note("Rows(body) for every verdict class — the live-SSE hot path is")
		note("K per-principal filters × 1 event, and the re-parse is shed")
	}
	blank()

	note("THREADS: a filter call is ALSO a use of its schema handle — two")
	note("filters over one schema never run concurrently (the SDK enforces it)")
	note("LIFETIME: filters AND blocks free before their schema; Close()")
	note("ordering is structural in every SDK — schema Close frees them first")
	note("ENFORCEMENT GATE: nothing may enforce read-side security on this")
	note("API until the WHERE-truth rig gates green (zero over-admit, zero")
	note("over-hide). Until that run of record exists this is a shadow/replay")
	note("surface: log disagreements, enforce with what enforced yesterday —")
	note("the twin is a call shape, not an enforcement opening.")
}

// verdictString renders a FilterResult's verdicts as the document's compact
// t/f/e/d string.
func verdictString(fr chtypes.FilterResult) string {
	if fr.Outcome != chtypes.FilterOK {
		return fmt.Sprintf("(call-level: outcome=%v code=%d %s)", fr.Outcome, fr.ErrCode, truncate(fr.ErrMsg, 40))
	}
	var b strings.Builder
	for _, v := range fr.Verdicts {
		b.WriteString(v.String())
	}
	return "\"" + b.String() + "\""
}

// ---------------------------------------------------------------- plumbing
//
// Everything below is printing helpers — no chtypes calls hide here.

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
func feed(s *chtypes.LoadedSchema, label string, f chtypes.Format, body []byte) {
	b, err := s.Rows(f, body, nil)
	if err != nil {
		kv("  "+label, "call failed: "+err.Error())
		return
	}
	parts := []string{b.Outcome.String()}
	if b.ErrCode != 0 {
		parts = append(parts, "code="+strconv.Itoa(b.ErrCode))
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
		if t.Lossy() {
			lossy = ", LOSSY"
		}
		raw(fmt.Sprintf("      ~ %s: %s -> %s (%s%s)", t.Column, t.Input, t.Stored, t.Reason, lossy))
	}
}

// describeError prints which of the two peer types an error is, with its code.
func describeError(err error, what string) {
	var se *chtypes.SchemaError
	var ue *chtypes.UnsupportedError
	switch {
	case errors.As(err, &ue):
		kv(what, "*UnsupportedError  (a DECLINE)")
		kv("  message", truncate(ue.Msg, 84))
		kv("  also a *SchemaError?", fmt.Sprintf("%v   <- peers, not a hierarchy", errors.As(err, &se)))
	case errors.As(err, &se):
		kv(what, fmt.Sprintf("*SchemaError  (a REFUSAL), .Code=%d", se.Code))
		kv("  message", truncate(se.Msg, 84))
		kv("  also an *UnsupportedError?", fmt.Sprintf("%v   <- peers, not a hierarchy", errors.As(err, &ue)))
	case err == nil:
		kv(what, "(no error)")
	default:
		kv(what, fmt.Sprintf("%T: %v", err, err))
	}
}

// classify names an error's KIND in one word (used where the full describe
// would drown the section).
func classify(err error) string {
	var se *chtypes.SchemaError
	var ue *chtypes.UnsupportedError
	switch {
	case err == nil:
		return "accepted"
	case errors.As(err, &ue):
		return "DECLINED  (*UnsupportedError)"
	case errors.As(err, &se):
		return "REFUSED   (*SchemaError, code " + strconv.Itoa(se.Code) + ")"
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
	return b.Outcome.String()
}

func verdictRow(r chtypes.RowResult) string {
	parts := []string{r.Outcome.String()}
	for _, v := range r.Values {
		parts = append(parts, fmt.Sprintf("%s=%s(%s)", v.Column, textOr(v), v.Source))
	}
	return strings.Join(parts, "  ")
}

func textOr(v chtypes.Value) string {
	if v.Text == "" && !v.Null {
		return "<unreadable>"
	}
	return v.Text
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
	return err.Error()
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

func unhex(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		panic(err)
	}
	return b
}

// registryDir is $CHTYPES_REGISTRY, else the per-user artifact cache every
// SDK defaults to (chtypes.DefaultRegistryDir).
func registryDir() string {
	if dir := os.Getenv("CHTYPES_REGISTRY"); dir != "" {
		return dir
	}
	return chtypes.DefaultRegistryDir()
}

// newestLine picks the numerically highest minor line: "25.10" > "25.3",
// which a string sort gets exactly backwards.
func newestLine(lines []string) string {
	best := ""
	for _, l := range lines {
		if best == "" || lineLess(best, l) {
			best = l
		}
	}
	return best
}

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
