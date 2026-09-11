// Package chtypes is ClickHouse's own C++ type machinery, vendored, wrapped
// in a C API, bound to Go via cgo.
//
// The surface is docs/reference/bindings.md; the C contract underneath is the C ABI contract.
// Everything semantic — type parsing, canonicalization, coercion, error codes
// — is executed by real ClickHouse code compiled from the pinned release, so
// it is exact by construction rather than reimplemented. Nothing in this
// package decides what a value coerces to.
//
// # The three outcomes, and the two error types
//
// Every answer this package gives is one of three distinct things, and
// conflating any two is a correctness bug in the caller:
//
//   - REJECTED — the server itself would refuse this row or DDL. On the
//     schema paths it surfaces as *SchemaError, whose Code is always a real
//     ClickHouse error code (50 unknown type, 115 unknown setting, ...). On
//     the row paths it is RowResult.Outcome == Rejected with ErrCode/ErrMsg.
//   - UNSUPPORTED — this build declines to answer; a real server might well
//     have accepted it, so the caller must fall back to the server rather
//     than refuse the tenant. On the schema paths it surfaces as
//     *UnsupportedError, a PEER of SchemaError that errors.As(&SchemaError{})
//     never matches. On the row paths it is Outcome == Unsupported.
//   - ACCEPTED — possibly with silent coercions (RowResult.Transformed),
//     library-resolved volatile DEFAULTs (RowResult.Substituted — the caller
//     MUST send those columns explicitly), and MATERIALIZED values
//     (RowResult.Computed). AcceptedPoisoned is still an ACCEPTED insert,
//     whose stored value no later SELECT can read back (code 691).
//
// The one thing ClickHouse does not provide is Transformed: it never reports
// "I silently changed your value". That is derived here, from a second parse of
// the same bytes through a widened reference type — see transform.go.
//
// # DEFAULT expressions
//
// Every DEFAULT is evaluated by ClickHouse's own machinery, entered at the two
// points the `format()` table function uses: parseColumnsListFromString for the
// schema and evaluateMissingDefaults for the value. That matters because a
// DEFAULT is applied through CAST, not through the format reader, and the two
// disagree on 53 of 98 measured (type, literal) pairs: `DEFAULT '2107-01-01'`
// into DateTime saturates to 2106-02-07 where the same literal arriving as
// JSON wraps to 1970-11-24, and `DateTime64(3) DEFAULT 1705312200` is SECONDS
// where the same number as data is TICKS.
//
// # Volatile DEFAULTs, and what the caller still owes
//
// now(), now64(n), today(), yesterday() — and any expression over them — are
// resolved BY THIS LIBRARY, against ONE INSTANT PER BATCH, and reported in
// RowResult.Substituted.
//
// The caller must do exactly one thing with that list: send every column in it
// as an explicit value in the INSERT. That is not a convenience, it is the
// whole mechanism. If the server is left to evaluate the expression, preview
// and stored differ always at now64 resolution and sometimes at now()
// resolution — measured on a real 25.8 server, a 200-row batch at
// max_insert_block_size=10 stamps 20 distinct now64(9) values, because
// ClickHouse reads the clock once per BLOCK, not once per insert. Sending the
// library's own values makes preview == stored by construction, and gives a
// stronger guarantee than the server offers: one instant for the whole batch.
//
// A batch is one Rows() call. Two Rows() calls get two instants, and the
// library makes no claim across them.
//
// # What substitution disarms
//
// Supplying a column means the server never evaluates its DEFAULT — and that
// silently switches off everything attached to that expression. Measured:
//
//	x Int64 DEFAULT throwIf(1,'boom')     absent: 395   supplied: ACCEPTED
//	x Int64 DEFAULT <a Nullable column>   absent: 349   supplied: ACCEPTED
//	x Int64 DEFAULT sleep(0.01)           absent: runs  supplied: not evaluated
//
// So a schema that could never take a row starts taking rows. That is usually
// what the tenant wants and is a second argument for substitution, but it is a
// behavior change the caller is making on the tenant's behalf, and it is why
// every substitution is also reported as a Transform with reason
// "default_materialized" rather than passed over in silence.
//
// # Clock skew: what this library tolerates, and what happens past it
//
// The library reads the LOCAL clock. It knows nothing about the server's.
// Three settings, passed like any other on the Rows/RowWithSettings call, are
// the caller's whole interface to that gap:
//
//	chtypes_now_epoch_nanos      pin the batch instant outright (tests, replay)
//	chtypes_clock_offset_nanos   the caller's measured (server - client) offset,
//	                             added to every clock read
//	chtypes_max_clock_skew_nanos refuse to substitute when |offset| exceeds this
//
// With no settings the tolerated skew is UNBOUNDED and the library will happily
// stamp whatever the local clock says. That is safe only because of what the
// value is used for, and the caller — not this library — knows that:
//
//	CHECK ts <= now()          rejects at ANY positive skew: code 469, measured
//	                           at +5 s, +60 s and +3600 s. Loud, and harmless.
//	PARTITION BY toYYYYMMDD(ts) files the row in the client's day. No signal.
//	TTL ts + INTERVAL ...      a too-OLD timestamp is accepted, previewed as
//	                           accepted, and then SILENTLY DELETED at merge
//	                           time. This is the worst outcome in the system:
//	                           there is no error, no code, and no moment at
//	                           which anything is wrong until the row is gone.
//
// So the rule is: if the tenant's table has a TTL, a PARTITION BY, or a
// CONSTRAINT over a volatile-DEFAULT column, the substituted value is
// load-bearing. Measure the server offset, pass it as
// chtypes_clock_offset_nanos, refresh it on a timer, and set
// chtypes_max_clock_skew_nanos to the slack the schema actually allows — zero
// for `CHECK ts <= now()`, the window width for a bounded CHECK. Past the
// budget the library returns Unsupported rather than inventing a timestamp the
// server would not have written. This library cannot detect those clauses: it
// is given a column list, not a CREATE TABLE.
// # Statelessness — the contract, and why DEFAULT semantics never break it
//
// Every function in this package is a pure function of five inputs:
//
//	(ClickHouse version, schema text, row bytes, settings, clock instant)
//
// There is no cross-call state. A CompiledSchema is derived data — recompiling
// the same DDL on another instance yields a plan that produces byte-identical
// answers; the process-wide pieces (the global Context, the evaluation-guard
// pool, interned registries) are caches whose contents never influence a
// verdict or a value. Two gateway instances given the same five inputs agree
// without ever having met, which is what makes the edge horizontally scalable
// with no coordination tier.
//
// DEFAULT semantics cannot smuggle state in, and that is verified per build
// rather than believed: ClickHouse has no auto-increment or sequence DEFAULT.
// A DEFAULT expression can reference only same-row columns and functions, and
// tools/gen_function_flags.py enumerates every function in this build's own
// registry (build/function_flags.tsv) and fails the build unless the volatile
// set the classifier admits is exactly the four clock reads. The closest
// things ClickHouse has to a sequence — generateSerialID (Keeper-backed; its
// resolver throws code 139 NO_ELEMENTS_IN_CONFIG off a server) and
// generateSnowflakeID (non-deterministic) — are refused by the same flags that
// refuse rand(). AUTO_INCREMENT as a column clause is rejected by ClickHouse's
// own parser path (SYNTAX_ERROR). So nothing an admitted DEFAULT can express
// requires cross-instance coordination; the only impurity is the clock, and
// the clock is an explicit INPUT (chtypes_now_epoch_nanos pins it, offset and
// budget bound it) rather than hidden state.
//
// # Resource envelope — bounded evaluation, decided at admission
//
// Evaluating a tenant's DEFAULT runs their expression in this process, and an
// expression can be a resource bomb: DEFAULT range(100000000) measured
// 13.99 GB RSS and 14.8 s for one row before the envelope existed. The
// owner's design gates at ParseSchema — the rare path — so CompileDDL probes
// every DEFAULT under two ceilings and refuses the SCHEMA, naming the column
// and the budget, when one fires:
//
//	chtypes_default_eval_memory_bytes  (default 256 MiB; 0 disables)
//	chtypes_default_eval_wall_nanos    (default 1 s;     0 disables)
//
// both set process-wide via SetDefaultSettings — admission deliberately takes
// no per-call settings. The memory ceiling is enforced mid-allocation through
// ClickHouse's own MemoryTracker on a private per-evaluation tracker (code 241
// from inside the allocation, cleanly unwound); the wall ceiling is detection
// after the fact, and sleep() specifically is refused through upstream's own
// function_sleep_max_microseconds_per_block=1 at zero wall cost.
//
// One admitted shape still needs the guard at row time, and keeps it: a
// DEFAULT that references another column (range(a)) is cheap at admission —
// probed with the column's type default — and unbounded only for a particular
// row's value. Such a row is refused (Unsupported, never a fabricated
// rejection) under the same ceilings. No probe at admission can close this
// hole: range(4000000000 - a) inverts any worst-case value the probe picks.
//
// Platform: the ceiling covers ClickHouse's Allocator-routed memory — column
// buffers, arenas, hash tables, i.e. every mechanism by which an expression's
// RESULT gets large — on every platform, because Allocator calls the tracker
// explicitly. On Linux the mandatory new_delete archive additionally routes
// plain operator-new allocations into the same tracker; on macOS those stay
// unseen. A bomb made purely of small heap objects would need an enormous
// expression to describe, and schema text is capped at 256 KiB by
// DBMS_DEFAULT_MAX_QUERY_SIZE, so every runtime amplification must come out of
// a function and land in a column — which is tracked. macOS is the development
// platform; Linux, the shipping platform, has the belt and the braces.
package chtypes

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// Version selects which ClickHouse semantics to emulate, e.g. "25.10.7.6".
//
// A linked build vendors exactly one release. A Version this build was not
// compiled from is an error rather than a silent approximation: asking for
// 26.6 semantics from a 25.8 binary would be a lie, and the rigs score
// silent wrongness hardest. (The multi-version Registry lifts this by
// dlopen'ing one artifact per version — see multiversion.go.)
type Version string

// Schema is one tenant table's column list, in declaration order. It is the
// structured input to ParseSchema; the validated, canonicalized form is
// CompiledSchema.
type Schema struct {
	Columns []Column
}

// Column is one column declaration. As an INPUT (Schema handed to
// ParseSchema) the fields are the caller's spelling; as an OUTPUT
// (CompiledSchema.Columns, LoadedSchema.Columns) they are ClickHouse's own
// canonical spelling — Type as the canonical type expression, Default as the
// canonicalized DEFAULT expression source.
type Column struct {
	Name        string
	Type        string // ClickHouse type expression, e.g. "Nullable(Decimal(18,4))"
	Default     string // DEFAULT expression source, "" if none
	DefaultKind DefaultKind
}

// DefaultKind classifies a column's DEFAULT clause, mirroring
// chs_schema_column_default_kind's five answers ("", "DEFAULT",
// "MATERIALIZED", "ALIAS", "EPHEMERAL").
type DefaultKind int

const (
	// KindNone: the column declares no DEFAULT clause.
	KindNone DefaultKind = iota
	// KindDefault: an ordinary DEFAULT — applied when the row omits the column.
	KindDefault
	// KindMaterialized: computed at insert and durable, but not part of
	// SELECT * — reported via RowResult.Computed, never in Values.
	KindMaterialized
	// KindAlias: computed at READ time; retroactively changed by ALTER, so
	// never presented as a stored value (src "skipped").
	KindAlias
	// KindEphemeral: readable only through an INSERT with an explicit column
	// list, which a format stream cannot carry — see docs/reference/bindings.md
	// §EPHEMERAL for the mispreview a gateway must decline.
	KindEphemeral
)

// String returns the DDL keyword ("DEFAULT", "MATERIALIZED", "ALIAS",
// "EPHEMERAL"), or "" for KindNone.
func (k DefaultKind) String() string {
	switch k {
	case KindDefault:
		return "DEFAULT"
	case KindMaterialized:
		return "MATERIALIZED"
	case KindAlias:
		return "ALIAS"
	case KindEphemeral:
		return "EPHEMERAL"
	}
	return ""
}

func parseDefaultKind(s string) DefaultKind {
	switch strings.ToUpper(s) {
	case "DEFAULT":
		return KindDefault
	case "MATERIALIZED":
		return KindMaterialized
	case "ALIAS":
		return KindAlias
	case "EPHEMERAL":
		return KindEphemeral
	}
	return KindNone
}

// Format selects the input encoding of a row. The numeric values are the C
// ABI's enum chs_format codes and are FROZEN — bindings pass the integers
// across the boundary (the C ABI contract §Types and schemas), so they must never
// be renumbered.
//
// CSV, TSV, Values and JSONCompactEachRow are POSITIONAL: the k-th field
// addresses the k-th insertable column (MATERIALIZED/ALIAS/EPHEMERAL occupy
// no position). JSONEachRow and Native address columns by NAME. The RowBinary
// family, Native and Buffers are binary and all-or-nothing per batch.
type Format int

const (
	// JSONEachRow is one JSON object per row, name-addressed.
	JSONEachRow Format = iota
	// CSV is comma-separated, positional; a quoted field can contain newlines.
	CSV
	// TSV (TabSeparated) is positional; `\N` is null. The one text format
	// whose result documents carry the wire round-trip detector (see
	// Transform).
	TSV
	// Values is the INSERT ... VALUES literal syntax, positional.
	Values
	// JSONCompactEachRow is one JSON array per row, positional.
	JSONCompactEachRow
	// RowBinary reads values in ClickHouse's own storage encoding via the
	// vendored ISerialization::deserializeBinary — the reader
	// BinaryRowInputFormat uses. Framing faults are one code (33), batches
	// are all-or-nothing, and input_format_allow_errors_* never applies
	// Requires an artifact built at or after the
	// RowBinary exposure; older artifacts reject with "unknown format".
	RowBinary
	// RowBinaryWithDefaults adds the measured per-column marker byte: any
	// nonzero byte means "compute the column's DEFAULT, read no value bytes".
	RowBinaryWithDefaults
	// RowBinaryWithNamesAndTypesAndDefaults (ClickHouse 26.x+) prefixes a
	// LEB128 column count plus name and type strings; earlier vendored trees
	// answer 73 UNKNOWN_FORMAT, exactly as their servers do.
	RowBinaryWithNamesAndTypesAndDefaults
	// Native is COLUMN-oriented — per block a column count, a row count, then
	// each column's name, type expression and whole serialized body — and it
	// is what every ClickHouse client library sends on INSERT. It is modeled
	// at the revision `INSERT ... FORMAT Native` uses (0), so there is no
	// BlockInfo prefix and no per-column serialization-kind byte; blocks taken
	// off a live TCP connection carry both and are a different contract
	// (the C ABI contract §Native).
	//
	// Because the stream carries names and types, the declared schema and the
	// payload can disagree — and ClickHouse's own resolution is not uniformly
	// an error: a TYPE mismatch is CAST by default, an unknown column NAME is
	// dropped or refused depending on input_format_skip_unknown_fields, and a
	// column the block omits is fine with rows and a LOGICAL_ERROR without.
	// Requires an artifact built at or after the Native exposure; older ones
	// reject with "unknown format".
	Native
	// Buffers (ClickHouse 26.5+) is Native's column encoding under a
	// length-prefixed frame that carries NO names and NO types: per block a
	// uint64le column count, a uint64le row count, then per column a uint64le
	// byte size followed by that column's bytes exactly as Native writes them
	// (src/Formats/BuffersReader.h:13-24).
	//
	// The missing names and types are the whole point. Native can reconcile a
	// producer against a consumer; Buffers cannot, so a schema disagreement of
	// EQUAL width is undetectable in band and lands as a silently reinterpreted
	// value — a UInt32 producer's 4294967295 stored as -1 by an Int32 consumer.
	// Only a WIDTH disagreement is caught, because the declared byte size then
	// fails to match what the declared type consumed. This library reports what
	// the server reports; the bytes do not carry enough to report more.
	//
	// Earlier vendored trees answer 73 UNKNOWN_FORMAT, exactly as their servers
	// do. Requires an artifact built at or after the Buffers exposure; older
	// ones reject with "unknown format".
	Buffers
)

// ExportNone is RowsExport's "no export requested" sentinel — the C ABI's
// CHS_EXPORT_NONE (-1). It is NOT a member of enum chs_format and is legal
// only as RowsExport's exportFormat argument, where it means "give me the
// document shape docFlags selects, emit no bytes". Rows() passes it
// internally, which is what keeps Rows() byte-identical to revision 2.
const ExportNone Format = -1

// DocFlags selects which document GROUPS the per-row documents carry
// (the C ABI contract §Document flags). The verdict channel — batch and per-row
// outcome/code/err, rows_read, rows_skipped, unsupported_settings,
// engine_rows, storage_transforms — is ALWAYS emitted and is not a flag.
// DocAll reproduces the full document Rows() returns, byte-for-byte at the C
// layer; 0 is "lean" (verdicts only: Values, Transformed, Substituted,
// Computed and UnknownFields all come back empty). A bit outside DocAll is
// refused loudly by the library (the whole call answers unsupported) — pass
// flags through, never pre-validate them here.
//
// The cost asymmetry, so callers can reason (the C ABI contract §Document flags):
// DocValues without DocTransforms skips the reference second-parse and the
// wire round trip C-side — real compute saved, and detectors 2/3 have
// nothing to run on (ref is null, no wire; that is the caller's choice, not
// data loss). DocTransforms without DocValues still computes both and saves
// only bytes: cols[] is filtered to the entries a change detector could fire
// on (the conservative byte-equality retention rule), each with its full
// field set, so Transformed is derived by the same detectors as always.
type DocFlags uint

const (
	// DocValues carries the full cols[] array (per-column stored text and
	// value provenance) and unknown_fields.
	DocValues DocFlags = 0x1
	// DocTransforms carries the change-detection channel: the reference
	// second-parse (ref/ref_type) and the TSV wire round trip run and are
	// emitted; without DocValues, cols[] keeps only the entries a spec'd
	// detector could fire on — always with the full field set.
	DocTransforms DocFlags = 0x2
	// DocDefaults carries computed[] (MATERIALIZED values) and, without
	// DocValues, the src == "default_substituted" cols[] entries — the
	// volatile-DEFAULT substitutions a caller must echo into its INSERT.
	DocDefaults DocFlags = 0x4
	// DocAll is today's full document — what Rows() always requests.
	DocAll DocFlags = DocValues | DocTransforms | DocDefaults
)

// Span addresses one row's line inside an export Payload: Payload[Off:Off+Len]
// IS that row's complete serialized line, terminating '\n' included, and is
// itself a valid one-row body in the export format. Spans are index-aligned
// with BatchResult.Rows; a non-accepted row (rejected/skipped/unsupported)
// carries {0, 0}. The concatenation of all non-zero spans reproduces Payload
// exactly, which is what lets batches merge by byte concatenation.
type Span struct {
	Off int `json:"off"`
	Len int `json:"len"`
}

// Outcome is the verdict on one row (or one batch). Key on it, never on the
// error code alone: a row-level Unsupported can carry ErrCode 0
// (the C ABI contract §Top-level fields).
type Outcome int

const (
	// Accepted: the server would take this row — possibly with silent
	// coercions, reported in RowResult.Transformed.
	Accepted Outcome = iota
	// Rejected: the server itself would refuse this row; ErrCode carries the
	// server's own ClickHouse error code and ErrMsg its message.
	Rejected
	// AcceptedPoisoned: ClickHouse accepts the insert but the stored value
	// cannot be read back — the null-into-Enum8 class, where every later
	// SELECT fails with code 691.
	AcceptedPoisoned
	// Unsupported is a chtypes extension to the spec, and is deliberate: a case
	// this build refuses to answer must never be scored as agreement. It is
	// returned for expression DEFAULTs and for type families that require a
	// live ClickHouse Context.
	Unsupported
	// Skipped: the row was dropped under input_format_allow_errors_* and the
	// batch continued — the server's own skip, itemized (2026-08-27). ErrCode
	// and ErrMsg carry the error IRowInputFormat::generate caught before
	// resyncing, verbatim; Values is empty (the row is NOT stored). Only ever
	// seen on rows inside a BatchResult, never as a batch verdict and never
	// from Row. A consumer rendering survivors must filter on it.
	Skipped
)

// String returns the result-document spelling: "accepted", "rejected",
// "accepted_poisoned", "unsupported", "skipped".
func (o Outcome) String() string {
	switch o {
	case Accepted:
		return "accepted"
	case Rejected:
		return "rejected"
	case AcceptedPoisoned:
		return "accepted_poisoned"
	case Unsupported:
		return "unsupported"
	case Skipped:
		return "skipped"
	}
	return "unknown"
}

// Value is one coerced column value, as ClickHouse itself renders it.
//
// Text is ClickHouse's JSON rendering of the stored value (`0`, `"2023-11-14
// 22:13:20"`, `[1,0,3]`) — the same bytes the server would emit for the row,
// which is what makes it directly comparable against a real ClickHouse.
type Value struct {
	Column string
	Text   string
	Null   bool
	// Source records where the value came from:
	//   "input"                a value the row supplied
	//   "default"              a DEFAULT expression this library evaluated
	//                          through ClickHouse's own CAST path
	//   "default_substituted"  a VOLATILE DEFAULT (now()/now64(n)/today()/
	//                          yesterday(), or an expression over one) that this
	//                          library resolved locally — the caller MUST send
	//                          it as an explicit column; see RowResult.Substituted
	//   "absent"               the type's own default, no DEFAULT declared
	//   "skipped"              MATERIALIZED / ALIAS / EPHEMERAL, never read
	//                          from an input row
	Source string
}

// String returns Text — ClickHouse's own JSON rendering of the stored value.
func (v Value) String() string { return v.Text }

// Transform records a silent change ClickHouse made on the way to storage:
// input 256 into UInt8 stored as 0, reason "overflow_wrap". Reason is one of
// the stable Reason* strings below; Lossy reports whether information was
// lost (only reformat / default_filled / zero_filled / default_materialized
// are non-lossy). Reporting these is the core product guarantee — a binding
// that drops them hides exactly the changes a tenant needs warning about.
type Transform struct {
	Column string `json:"column"`
	Input  string `json:"input"`
	Stored string `json:"stored"`
	Reason string `json:"reason"` // "overflow_wrap", "date_clamp", …
	// Row is the 0-based index of the row inside the request body. A batch of
	// 10 rows with one bad value is useless to a tenant without it.
	Row int `json:"row"`
}

// Lossy reports whether this transformation lost information, as opposed to
// only changing how the value is written (1700000000 -> "2023-11-14 22:13:20").
func (t Transform) Lossy() bool { return t.lossyReason() }

// RowResult is the outcome of validating and coercing one row.
type RowResult struct {
	Values      []Value     // canonical coerced values, positional
	Transformed []Transform // values ClickHouse would silently change
	Outcome     Outcome
	// ErrCode is ClickHouse's own code when Rejected, and 691 when
	// AcceptedPoisoned. Key on Outcome, never on the code alone: a row-level
	// Unsupported carries ErrCode 0 (the sentinel is the Outcome itself).
	ErrCode int
	ErrMsg  string // ClickHouse error message when Rejected
	// UnknownFields lists input fields with no matching column.
	UnknownFields []string
	// UnsupportedSettings lists requested settings this build does not model;
	// non-empty means the answer must not be scored as agreement.
	UnsupportedSettings []string
	// Substituted lists the columns whose VOLATILE DEFAULT this library
	// resolved itself, against one instant per batch.
	//
	// THE CALLER MUST SEND EVERY ONE OF THESE AS AN EXPLICIT COLUMN. The value
	// here is what a preview shows the tenant, and it is only the truth if the
	// server is never asked to evaluate the expression: measured, preview and
	// insert never agree on now64(3) (2–60 ms apart even back to back), and
	// disagreed on now() in 1 of 4 samples at a 0.5 s gap.
	//
	// Sending the column also *disarms* the server-side machinery attached to
	// it — see "What substitution disarms" in the package documentation. This
	// list is the caller's cue to warn, not only to serialize.
	Substituted []Substitution
	// Computed carries the MATERIALIZED columns' values for this row.
	//
	// They are NOT in Values, because `SELECT *` does not return them: putting
	// them in the stored row would make the preview disagree with what an SSE
	// subscriber reading the table sees. They are reported separately because
	// they are nonetheless DURABLE — an `ALTER ... MODIFY COLUMN m MATERIALIZED
	// <new expr>` does not touch rows already written — so a preview of one is
	// a promise the table keeps.
	//
	// ALIAS is deliberately absent. It is computable by exactly the same
	// machinery, and it is measured that `ALTER ... MODIFY COLUMN a ALIAS <new
	// expr>` RETROACTIVELY CHANGES what already-inserted rows read back as
	// An ALIAS is therefore a fact about
	// the schema at read time, not about the row, and this library will not
	// present one as a stored value. A caller that wants to show it must ask
	// the server, and must label it computed-at-read.
	Computed []Computed
}

// Computed is one MATERIALIZED column's value for a row: stored at insert,
// frozen thereafter.
type Computed struct {
	Column string
	Kind   string // always "MATERIALIZED" today; see RowResult.Computed
	Text   string
}

// Substitution is one volatile DEFAULT the library resolved instead of the
// server.
//
// Expr is the DEFAULT as ClickHouse canonicalized it; Text is the value
// rendered by ClickHouse's own serializer for the declared type, so sending it
// back as a JSON field round-trips to the identical stored value. Emit Text
// verbatim: a JSON *float* for a tick count is a hard reject (code 27), never a
// coercion.
type Substitution struct {
	Column string
	Expr   string
	Text   string
}

// SchemaError is a REFUSAL: ClickHouse itself said no to a type, a column
// list, an engine or a TTL. It names the offending column when the failure is
// attributable to one.
//
// Code is ALWAYS a real ClickHouse error code. "This build declines to answer"
// is a DIFFERENT TYPE — UnsupportedError — never this one carrying a sentinel,
// so no SchemaError ever holds CodeUnsupported (docs/reference/bindings.md rule 12).
type SchemaError struct {
	Column string // "" when the failure is not attributable to one column
	Code   int
	Msg    string
}

// Error renders as "chtypes: [<code>] <msg>", naming the column when one is
// attributable.
func (e *SchemaError) Error() string {
	if e.Column != "" {
		return fmt.Sprintf("chtypes: column %q: [%d] %s", e.Column, e.Code, e.Msg)
	}
	return fmt.Sprintf("chtypes: [%d] %s", e.Code, e.Msg)
}

// UnsupportedError is a DECLINE: "a real server might well have accepted this;
// I will not guess." It is never ClickHouse rejecting anything, so it carries
// no Code field at all — there is no code to carry.
//
// It is a PEER of SchemaError, deliberately NOT a subtype and deliberately not
// reachable through errors.As(&SchemaError{}). A decline that still satisfied
// "is a SchemaError" would be the sentinel problem wearing a type hierarchy:
// every caller that forgot to check the predicate would keep silently turning
// declines into rejections, which is a manufactured over-reject — data loss the
// product never made, budgeted at zero (the C ABI contract "Error model"). As a peer,
// forgetting produces an unhandled error, which is loud.
//
// Callers switch on the type, never on a code:
//
//	var ue *chtypes.UnsupportedError
//	var se *chtypes.SchemaError
//	switch {
//	case errors.As(err, &ue): // validate cautiously; do NOT blame the tenant
//	case errors.As(err, &se): // ClickHouse refused: se.Code is its own code
//	}
type UnsupportedError struct {
	Column string // "" when the decline is not attributable to one column
	Msg    string
}

// Error renders with the ABI's CodeUnsupported sentinel in the same shape a
// SchemaError renders its code. The sentinel is not a field of this type — it
// is the wire value the C ABI returned — but the RENDERING is frozen: the
// conformance drivers put this exact string on the protocol wire as an
// `unsupported` scope, and Python's UnsupportedError already spells it this
// way. Changing the text would move rig records without changing a verdict.
func (e *UnsupportedError) Error() string {
	if e.Column != "" {
		return fmt.Sprintf("chtypes: column %q: [%d] %s", e.Column, CodeUnsupported, e.Msg)
	}
	return fmt.Sprintf("chtypes: [%d] %s", CodeUnsupported, e.Msg)
}

// CodeUnsupported is the sentinel error code meaning "this build refuses to
// answer", as distinct from any ClickHouse error code. It stays exported
// because it is the ABI's wire value (CHS_CODE_UNSUPPORTED) and row-level
// results still carry it in RowResult.ErrCode; no error VALUE in this package
// carries it any more — UnsupportedError does.
const CodeUnsupported = -2

// schemaErr is the ONE place an ABI error code becomes a Go error, so the
// refusal/decline split cannot be decided differently in two files.
//
// The SIGN decides (docs/reference/bindings.md rule 12, the C ABI contract "Error model"):
// a positive code is the server's own refusal and rides through verbatim; any
// negative code is this library declining (-2 "I will not guess", -1 a guarded
// exception, -3 the dlopen shim's missing-symbol sentinel) and becomes an
// UnsupportedError. Keying on the sign rather than on == CodeUnsupported means
// a negative sentinel a later era adds can never become "a SchemaError with a
// negative Code", which the type's own contract forbids.
func schemaErr(code int, msg, column string) error {
	if code < 0 {
		return &UnsupportedError{Column: column, Msg: msg}
	}
	return &SchemaError{Column: column, Code: code, Msg: msg}
}

// Timezone is the server timezone assumed for bare DateTime/DateTime64
// columns. A stock ClickHouse container is UTC; without pinning it the host's
// TZ would leak into every result. Change it before the first call.
var Timezone = "UTC"

// ABIRevision is the chs_* ABI revision this package was COMPILED against —
// CHS_ABI_REVISION from chtypes.h, read through cgo so the two can never drift.
// The artifact reports its own with chs_abi_revision(); see the C ABI contract
// §ABI identity. A dlopen'd Library reports the loaded artifact's revision
// through Library.ABIRevision, which is 0 when the artifact predates the probe.
const ABIRevision = 4

// CompileMode selects how a settings profile handed to CompileDDL relates to
// the settings this build compiles under. Numeric values are part of the
// ABI (bindings pass an int), exactly like Format.
type CompileMode int

// CompileDeclared is the only mode this build accepts: every setting the
// profile names takes the caller's value; every setting it does not name
// keeps the library's own compile base. Any other value is refused loudly
// (an *UnsupportedError).
const CompileDeclared CompileMode = 0

// compileConfig is CompileDDL's assembled options, built from CompileOptions.
type compileConfig struct {
	settings map[string]string
	mode     CompileMode
}

// CompileOption configures CompileDDL's settings profile. The zero value of
// every option is CompileDDL's un-optioned behavior, so opts... can always
// be omitted.
type CompileOption func(*compileConfig)

// WithCompileSettings declares a DECLARED settings profile — the settings
// the deployment's server runs, fixed into the handle at compile time
// exactly as a real CREATE TABLE fixes them into the table (the C ABI contract
// §Compile-time settings). A nil or empty map is IDENTICAL to omitting the
// option entirely.
//
// Every declared name takes the caller's value; every undeclared setting
// keeps the library's permissive compile base — a partial profile can admit
// a schema the server might refuse, and can never fabricate a rejection. An
// unknown setting name fails the compile with the server's own code 115;
// chtypes_* keys are per-call keys, not ClickHouse settings, and land on the
// same 115. Per-call settings still govern row parsing, and only row
// parsing.
//
// The one compile-shape setting modeled today is flatten_nested: at "0" a
// Nested(a,b) column compiles to ONE Array(Tuple(...)) column named as
// declared, exactly as the server's CREATE does under that setting; every
// downstream shape (Columns, name lookup, positional arity, the RowBinary
// wire) follows the compiled shape.
func WithCompileSettings(settings map[string]string) CompileOption {
	return func(c *compileConfig) { c.settings = settings }
}

// WithCompileMode sets the compile mode. CompileDeclared (0) is the only
// defined value; anything else is refused loudly by the library
// (an *UnsupportedError), reserved for a future COMPLETE-profile mode.
func WithCompileMode(mode CompileMode) CompileOption {
	return func(c *compileConfig) { c.mode = mode }
}

// engineConfig is SetEngine's assembled options, built from EngineOptions.
type engineConfig struct {
	mergeTreeSettings map[string]string
}

// EngineOption configures SetEngine's MergeTree-namespace settings.
type EngineOption func(*engineConfig)

// WithMergeTreeSettings declares the table's MergeTree-NAMESPACE settings —
// the SETTINGS clause after the engine, which DB::Settings cannot carry
// (allow_nullable_key, allow_floating_point_partition_key, ...). A nil or
// empty map is IDENTICAL to omitting the option. Names are validated by the
// server's own MergeTreeSettings object: an unknown name answers the
// server's own code 115. A known name declared at a NON-default value is
// refused (an *UnsupportedError) — no MergeTree setting's behavior is modeled
// yet, and silently ignoring a declared value would mean the declared
// profile is not in force. Declared at the default is inert and accepted.
func WithMergeTreeSettings(settings map[string]string) EngineOption {
	return func(c *engineConfig) { c.mergeTreeSettings = settings }
}

// ---------------------------------------------------------------- filters

// Verdict is one row's answer from Filter.Rows. Two of the four states are
// ANSWERS and two are NOT, and the split is load-bearing: a caller enforcing
// visibility MUST fail closed (hide the row / fail the request) on
// VerdictError and VerdictDecline — collapsing either into "false the
// answer" inverts fail-closed into fail-open under NOT, the measured leak
// class (docs/reference/bindings.md §Revision 3). The zero value is VerdictDecline,
// so an unset or unknown verdict is fail-closed by construction.
type Verdict int

const (
	// VerdictDecline: this library declines to answer for this row — the row
	// cannot be parsed/coerced under the schema, is poisoned, or tripped the
	// admission envelope. NOT an answer; fail closed. Zero value on purpose,
	// and the state an unknown verdict character degrades to.
	VerdictDecline Verdict = iota
	// VerdictTrue: the predicate is non-NULL and non-zero for this row.
	VerdictTrue
	// VerdictFalse: false OR NULL — SQL's three-valued logic collapsed at
	// the WHERE boundary, computed by the vendored functions.
	VerdictFalse
	// VerdictError: the predicate THREW on this row's values (e.g.
	// NO_COMMON_TYPE 386 from `s = 257` over String). On a real server a
	// WHERE that throws fails the WHOLE query; NOT an answer; fail closed.
	VerdictError
)

// Answered reports whether this verdict is an ANSWER (true/false) rather
// than an error or a decline. A security-enforcing caller hides the row on
// !Answered().
func (v Verdict) Answered() bool { return v == VerdictTrue || v == VerdictFalse }

// String returns the result-document character: "t", "f", "e", "d".
func (v Verdict) String() string {
	switch v {
	case VerdictTrue:
		return "t"
	case VerdictFalse:
		return "f"
	case VerdictError:
		return "e"
	}
	return "d"
}

// FilterOutcome is the CALL-level verdict of Filter.Rows — whether
// evaluation completed at all; per-row failures live in the Verdicts, not
// here.
type FilterOutcome int

const (
	// FilterOK: evaluation completed; Verdicts has one entry per row.
	FilterOK FilterOutcome = iota
	// FilterRejected: a call-level failure with ClickHouse's own code — an
	// unknown setting name's 115, an unsplittable body's framing error, a
	// binary decode fault. Verdicts is empty: a malformed body yields no
	// partial answers.
	FilterRejected
	// FilterUnsupported: a call-level decline (-2), and the state an
	// unrecognized outcome spelling degrades to — never FilterRejected,
	// mirroring the unknown-outcome rule (docs/reference/bindings.md §RowResult).
	FilterUnsupported
)

// String is the wire spelling, as the filter result document carries it —
// the same vocabulary Outcome.String answers in, and the same one the Python
// and TypeScript bindings' FilterOutcome values already are. Outcome, Verdict
// and DefaultKind each had a String and this one did not, so a Go caller could
// print every verdict in the ABI except this one.
func (f FilterOutcome) String() string {
	switch f {
	case FilterOK:
		return "ok"
	case FilterRejected:
		return "rejected"
	case FilterUnsupported:
		return "unsupported"
	}
	return "unsupported"
}

// FilterRowError itemizes one 'e' or 'd' row: the row's 0-based index and
// the code and message, verbatim — ClickHouse's own for an 'e' row, this
// library's decline for a 'd' row.
type FilterRowError struct {
	Row  int    `json:"row"`
	Code int    `json:"code"`
	Msg  string `json:"err"`
}

// FilterResult is one Filter.Rows answer.
type FilterResult struct {
	// Outcome is the call-level verdict. On anything but FilterOK, Verdicts
	// is empty and Errors is nil.
	Outcome FilterOutcome
	ErrCode int
	ErrMsg  string
	// RowsRead counts the rows the splitter/decoder yielded.
	RowsRead int
	// UnsupportedSettings as everywhere: non-empty means the answer MUST NOT
	// be scored as agreement.
	UnsupportedSettings []string
	// Verdicts holds one entry per row, in input order, index-aligned with
	// the rows the reader consumed — the itemized-skips addressing contract
	// holds even across allow_errors-style resync tails.
	Verdicts []Verdict
	// Errors itemizes every VerdictError and VerdictDecline row.
	Errors []FilterRowError
}

// filterDoc mirrors the JSON chs_filter_rows returns.
type filterDoc struct {
	Outcome             string           `json:"outcome"`
	Code                int              `json:"code"`
	Err                 string           `json:"err"`
	RowsRead            int              `json:"rows_read"`
	UnsupportedSettings []string         `json:"unsupported_settings"`
	Verdicts            string           `json:"verdicts"`
	Errors              []FilterRowError `json:"errors"`
}

// filterResultOf turns a filter document into a FilterResult — ONE assembler
// for the linked and dlopen'd paths, like batchResultOf.
func filterResultOf(js string) (FilterResult, error) {
	var doc filterDoc
	if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
		return FilterResult{}, fmt.Errorf("chtypes: bad filter document: %w", err)
	}
	res := FilterResult{
		ErrCode: doc.Code, ErrMsg: doc.Err, RowsRead: doc.RowsRead,
		UnsupportedSettings: doc.UnsupportedSettings,
	}
	switch doc.Outcome {
	case "ok":
		res.Outcome = FilterOK
	case "rejected":
		res.Outcome = FilterRejected
	default:
		// "unsupported", and every spelling this binding does not know:
		// degrade to the decline arm, never the rejection arm.
		res.Outcome = FilterUnsupported
	}
	if res.Outcome != FilterOK {
		return res, nil
	}
	res.Verdicts = make([]Verdict, 0, len(doc.Verdicts))
	for _, c := range doc.Verdicts {
		switch c {
		case 't':
			res.Verdicts = append(res.Verdicts, VerdictTrue)
		case 'f':
			res.Verdicts = append(res.Verdicts, VerdictFalse)
		case 'e':
			res.Verdicts = append(res.Verdicts, VerdictError)
		default:
			// 'd', and any character this binding does not know: decline —
			// fail closed, mirroring the unknown-outcome rule.
			res.Verdicts = append(res.Verdicts, VerdictDecline)
		}
	}
	res.Errors = doc.Errors
	return res, nil
}

// FilterOption configures CompileFilter — the same variadic functional-option
// shape CompileDDL uses (docs/reference/bindings.md §One compile function: options are
// each language's own idiom, and this is Go's).
type FilterOption func(*filterConfig)

// filterConfig is CompileFilter's assembled options.
type filterConfig struct {
	params map[string]string
}

// WithFilterParams binds `{name:Type}` query parameters for the compile.
// Values are STRINGS, exactly as the server's own parameter channels carry
// them (`param_name=value` — the settings_json convention); each is
// deserialized by the DECLARED type's own reader and injected as a typed
// literal AFTER SQL parsing, so a value is never SQL text and NEVER needs
// hand-escaping — injection safety is by construction, not by escaping
// (the C ABI contract §Filters, Query parameters). Do not render values into the
// expression yourself.
//
// CHOOSE THE BRACE TYPE FOR THE VALUE'S DOMAIN. A bound value follows the
// brace type's own reader, which WRAPS an out-of-domain integer — {p:UInt8}
// given "256" binds 0 and matches every genuine zero (measured, uniform
// 24.8-26.7, server-matched) — while the same constant written as a literal
// PROMOTES (x = 256 over UInt8 is simply never true). A too-narrow
// parameter type silently matches the wrong rows: size the type for the
// tenant-supplied domain ({p:UInt64}, {p:String}) or validate the value
// before binding it. Malformed spellings refuse loudly (the server's 457
// for "-1"/"+7"/"007" as UInt8; 32 for ""). A name bound twice at the C
// boundary takes the LAST binding — the server's own insert_or_assign rule
// (unreachable through this map, stated for completeness).
//
// The compiled handle bakes the values in: identity is per
// (schema, expr, params), so changing a value means compiling a new Filter.
// A caller compiling filters from tenant-influenced values MUST bound its
// cache (an LRU keyed on schema generation + expr + params-hash) and its
// compile rate per principal — the key is attacker-influencable, so an
// unbounded cache is a memory DoS and an unmetered compile path is a CPU
// DoS.
func WithFilterParams(params map[string]string) FilterOption {
	return func(c *filterConfig) { c.params = params }
}

// BatchResult is the outcome of one request body, which may hold many rows.
//
// A body is not a row: WaveHouse ships JSONEachRow bodies with many rows per
// request, row separation is format-specific, and ClickHouse's
// input_format_allow_errors_num / _ratio decide whether a bad row is skipped or
// aborts the batch. Row() is the single-row convenience over this.
type BatchResult struct {
	// Rows holds one RowResult per row the reader consumed, in input order —
	// including (2026-08-27) rows dropped under input_format_allow_errors_*,
	// which appear as Outcome == Skipped with the server's own caught error
	// and no Values. A consumer rendering survivors must filter on the row
	// Outcome, never assume every entry is stored.
	Rows        []RowResult
	Outcome     Outcome
	ErrCode     int
	ErrMsg      string
	RowsRead    int // rows the reader consumed, failed ones included
	RowsSkipped int // rows dropped under input_format_allow_errors_*; each is itemized in Rows as Skipped
	// Transformed aggregates every silent change in the batch, tagged with the
	// row it came from — a gateway cannot tell a tenant what to fix otherwise.
	Transformed []Transform
	// EngineRows is the stored preview AFTER the table engine's insert-time
	// merge, present only when SetEngine declared a specialized engine and the
	// batch was accepted. Each element is one stored row as a rendered JSON
	// object. nil means no engine semantics were applied and Rows is the
	// preview, exactly as before.
	EngineRows []json.RawMessage

	// Payload is the export channel (RowsExport only; always nil from Rows).
	// The batch's accepted rows, serialized once by the transcription of
	// ClickHouse's own output writer for the requested export format, copied
	// out of the C buffer and freed before this call returns — no ownership
	// crosses the boundary. Three states, and the distinction is the ABI's
	// own (the C ABI contract §Rows):
	//
	//	nil            no export was requested, the export was DECLINED
	//	               (ExportDeclined then names the reason), or a
	//	               call-level verdict preempted the export machinery
	//	               entirely (the batch Outcome is then the reason and
	//	               ExportDeclined stays "").
	//	non-nil, empty an accepted batch with zero accepted rows — the
	//	               EMITTED-EMPTY case, distinguishable from a decline.
	//	non-nil bytes  the exported lines; slice per Spans.
	Payload []byte
	// Spans is index-aligned with Rows: Spans[i] addresses row i's line
	// inside Payload, {0,0} for a non-accepted row. nil when no bytes were
	// emitted.
	Spans []Span
	// ExportDeclined is the library's reason for withholding requested export
	// bytes (a non-accepted batch, the full-arity guard, a serialization
	// failure, output_format_json_validate_utf8) — "" when bytes were emitted
	// or no export was requested. A decline here is -2-class honesty, never a
	// server verdict.
	ExportDeclined string
}

type batchDoc struct {
	Outcome     string   `json:"outcome"`
	Code        int      `json:"code"`
	Err         string   `json:"err"`
	RowsRead    int      `json:"rows_read"`
	RowsSkipped int      `json:"rows_skipped"`
	Rows        []rowDoc `json:"rows"`
	// EngineRows: what the part will hold AFTER the table engine's insert-time
	// merge (optimize_on_insert), present only when SetEngine declared a
	// specialized engine. Each element is one stored row as a JSON object with
	// the same rendering as per-row stored values. May legitimately be shorter
	// than Rows (a SummingMergeTree dropping an all-zero row) or reordered
	// (the block is sorted by the sorting key before the part is written).
	EngineRows []json.RawMessage `json:"engine_rows"`
	// StorageTransforms: what the storage layer did to rows the type layer
	// accepted — a row past its table TTL (reason "ttl_expired", not stored),
	// a value past its column TTL (reason "ttl_column_expired", reset to the
	// column DEFAULT). Folded into BatchResult.Transformed.
	StorageTransforms []storageTransformDoc `json:"storage_transforms"`
	// RowSpans: present exactly when export bytes were emitted — one
	// {off,len} per rows[] entry, index-aligned (the C ABI contract §Rows).
	RowSpans []Span `json:"row_spans"`
	// ExportDeclined: present exactly when an export was requested and
	// withheld, carrying the reason; absent otherwise.
	ExportDeclined string `json:"export_declined"`
}

type storageTransformDoc struct {
	Row       int             `json:"row"`
	Column    string          `json:"column"`
	Reason    string          `json:"reason"`
	StoredRaw json.RawMessage `json:"stored"`
}

// rowDoc mirrors the JSON chs_row returns.
type rowDoc struct {
	Outcome             string    `json:"outcome"`
	Code                int       `json:"code"`
	Err                 string    `json:"err"`
	UnknownFields       []string  `json:"unknown_fields"`
	UnsupportedSettings []string  `json:"unsupported_settings"`
	Cols                []colDoc  `json:"cols"`
	Computed            []compDoc `json:"computed"`
}

type compDoc struct {
	Name      string          `json:"name"`
	Kind      string          `json:"kind"`
	StoredRaw json.RawMessage `json:"stored"`
}

type colDoc struct {
	Name    string `json:"name"`
	Type    string `json:"type"`
	Base    string `json:"base"`
	Src     string `json:"src"`
	Input   string `json:"input"`
	RefType string `json:"ref_type"`
	// Stored and Ref arrive as raw JSON values, not JSON strings. A ClickHouse
	// String column holds arbitrary bytes, and decoding those into a Go string
	// would silently replace invalid UTF-8 with U+FFFD — which would read as a
	// silent transformation that never happened. RawMessage keeps the bytes.
	StoredRaw json.RawMessage `json:"stored"`
	RefRaw    json.RawMessage `json:"ref"`
	Nullable  bool            `json:"nullable"`
	Poison    bool            `json:"poison"`
	NullInput bool            `json:"null_input"`
	// DupDropped: the row named this column more than once and ClickHouse
	// kept the first value, discarding the rest without a signal.
	DupDropped bool `json:"dup_dropped"`
	// RefUnclassified: the C layer found this column's leaf numeric/temporal
	// by ClickHouse's own predicate but had no reference-ladder entry for it,
	// so the precise detector could not run. The classifier must then treat a
	// visible change as lossy, never as `reformat` — over-reporting is noise,
	// under-reporting hides a loss (audit F4). Absent (false) from every
	// artifact whose build gate ran; see lib/tools/gen_reference_ladder.py.
	RefUnclassified bool `json:"ref_unclassified"`
	// Wire: the stored value written back out in the FIELD'S OWN text
	// vocabulary by ClickHouse's own serializer, emitted only where `Input` is
	// in that same vocabulary (TSV today; see the note in chtypes.cpp). It is
	// what lets detector 1 work at all in a text format, where the field text
	// is usually not a JSON value and the JSON comparison never runs.
	Wire *string `json:"wire"`
}

func (c colDoc) Stored() string {
	// A JSON null here is a real stored value (a Nullable column holding null),
	// not the absence of one. Only the poison case has no renderable value, and
	// that is flagged separately.
	if len(c.StoredRaw) == 0 || c.Poison {
		return ""
	}
	return string(c.StoredRaw)
}

func (c colDoc) Ref() string {
	if len(c.RefRaw) == 0 || string(c.RefRaw) == "null" {
		return ""
	}
	return string(c.RefRaw)
}

// quoteBareDenormals makes the wrapper's row/batch documents strictly valid
// JSON. ClickHouse's own serializeTextJSON writes IEEE denormals as the bare
// tokens `inf`, `-inf` and `nan` unless output_format_json_quote_denormals is
// set — which is faithful output, but not parseable by encoding/json, and a
// QBit column full of infinities took the whole document down ("invalid
// character 'i'", 18 arbiter cases). The stored TEXT is preserved exactly:
// only the quoting is added, outside JSON strings, so downstream consumers
// see the same bytes ClickHouse would have written.
func quoteBareDenormals(b []byte) []byte {
	// Fast path: nothing that could be a bare denormal token.
	if !bytes.Contains(b, []byte("inf")) && !bytes.Contains(b, []byte("nan")) {
		return b
	}
	var out []byte
	inStr := false
	for i := 0; i < len(b); i++ {
		c := b[i]
		if inStr {
			out = append(out, c)
			if c == '\\' && i+1 < len(b) {
				i++
				out = append(out, b[i])
			} else if c == '"' {
				inStr = false
			}
			continue
		}
		if c == '"' {
			inStr = true
			out = append(out, c)
			continue
		}
		// A value can start after one of these structural bytes.
		if len(out) > 0 {
			switch prev := out[len(out)-1]; prev {
			case ':', ',', '[', ' ', '\t', '\n':
			default:
				_ = prev
				out = append(out, c)
				continue
			}
		}
		rest := b[i:]
		tok := 0
		if bytes.HasPrefix(rest, []byte("-inf")) {
			tok = 4
		} else if bytes.HasPrefix(rest, []byte("inf")) {
			tok = 3
		} else if bytes.HasPrefix(rest, []byte("nan")) {
			tok = 3
		}
		if tok > 0 && (len(rest) == tok || rest[tok] == ',' || rest[tok] == '}' || rest[tok] == ']') {
			out = append(out, '"')
			out = append(out, rest[:tok]...)
			out = append(out, '"')
			i += tok - 1
			continue
		}
		out = append(out, c)
	}
	return out
}

// settingsJSON is the ONE place per-call settings become the JSON object the
// C ABI takes; both the linked and the dlopen'd paths use it.
func settingsJSON(settings map[string]string) string {
	if len(settings) == 0 {
		return "{}"
	}
	b, _ := json.Marshal(settings)
	return string(b)
}

// batchResultOf turns a batch document into a BatchResult. ONE assembler for
// both the linked and the dlopen'd paths, deliberately: the dlopen'd 25.8
// artifact once kept a SummingMergeTree zero-row that the linked build
// dropped, and the only difference was a second, drifted copy of exactly
// this code.
func batchResultOf(js string) (BatchResult, error) {
	var doc batchDoc
	if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
		return BatchResult{}, fmt.Errorf("chtypes: bad batch document: %w", err)
	}
	res := BatchResult{
		Outcome: outcomeOf(doc.Outcome), ErrCode: doc.Code, ErrMsg: doc.Err,
		RowsRead: doc.RowsRead, RowsSkipped: doc.RowsSkipped,
		EngineRows: doc.EngineRows,
		Spans:      doc.RowSpans, ExportDeclined: doc.ExportDeclined,
	}
	for i, rd := range doc.Rows {
		rr := rowResultOf(rd)
		for j := range rr.Transformed {
			rr.Transformed[j].Row = i
		}
		res.Transformed = append(res.Transformed, rr.Transformed...)
		res.Rows = append(res.Rows, rr)
	}
	for _, st := range doc.StorageTransforms {
		res.Transformed = append(res.Transformed, Transform{
			Row: st.Row, Column: st.Column, Reason: st.Reason,
			Stored: string(st.StoredRaw),
		})
	}
	return res, nil
}

// outcomeOf maps a document's outcome string. An outcome this binding does
// not recognize degrades to Unsupported, never to Rejected: a future
// artifact's new verdict is by definition an answer this binding cannot
// interpret, and Unsupported is the arm that is never scored as agreement,
// while a default of Rejected would manufacture an over-reject — the
// zero-budget failure — out of pure vocabulary drift (docs/reference/bindings.md
// §RowResult, rule added 2026-08-26).
func outcomeOf(s string) Outcome {
	switch s {
	case "accepted":
		return Accepted
	case "rejected":
		return Rejected
	case "accepted_poisoned":
		return AcceptedPoisoned
	case "unsupported":
		return Unsupported
	case "skipped":
		return Skipped
	}
	return Unsupported
}

// rowResultOf turns one row document into a RowResult.
func rowResultOf(doc rowDoc) RowResult {
	res := RowResult{
		ErrCode: doc.Code, ErrMsg: doc.Err,
		UnknownFields: doc.UnknownFields, UnsupportedSettings: doc.UnsupportedSettings,
		Outcome: outcomeOf(doc.Outcome),
	}
	if len(doc.UnsupportedSettings) > 0 && res.Outcome != Rejected {
		res.Outcome = Unsupported
	}
	for _, c := range doc.Cols {
		if c.Src == "skipped" {
			continue
		}
		res.Values = append(res.Values, Value{
			Column: c.Name, Text: c.Stored(),
			Null:   string(c.StoredRaw) == "null" && !c.Poison,
			Source: c.Src,
		})
		if c.Src == "default_substituted" {
			res.Substituted = append(res.Substituted, Substitution{
				Column: c.Name, Expr: c.Input, Text: c.Stored(),
			})
		}
		res.Transformed = append(res.Transformed, classify(c)...)
	}
	for _, m := range doc.Computed {
		res.Computed = append(res.Computed, Computed{
			Column: m.Name, Kind: m.Kind, Text: string(m.StoredRaw),
		})
	}
	return res
}
