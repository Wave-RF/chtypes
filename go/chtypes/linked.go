//go:build chtypes_linked

// linked.go — the STATICALLY LINKED path: package-level CompileDDL /
// ValidateType / ParseSchema over the one artifact cgo links out of
// lib/build at compile time. It is a development and rig instrument (the
// conformance oracle answers for its linked version through it), not the
// path a consumer takes: without the `chtypes_linked` build tag this
// package is dlopen-only (multiversion.go) and needs no lib/build, no
// chtypes.h and no -lchtypes on the link line. The frozen numbers the
// dlopen-only build hardcodes (Format, DocFlags, CodeUnsupported,
// ExportNone, ABIRevision) are asserted against the header in
// linked_abi_check.go, so the two builds cannot drift apart silently.
package chtypes

/*
// The native library keeps its historical file name `libchtypes` — it is
// baked into every prebuilt per-version artifact (hours of C++ compute
// each) and into their manifests, so renaming it would mean rebuilding
// them all for zero behavioural change.
//
// The header is this repository's own include/chtypes.h — the SDK owns the
// contract; the core repository (the C++ wrapper and its build machinery)
// carries a copy it asserts identical at build time. WHERE the library is
// comes from outside: this file names only `-lchtypes`, and the caller that
// wants the linked path supplies the search path and a build-tree rpath
// through CGO_LDFLAGS (the core repo's ci/steps/_lib.sh `linked_env` sets
// them to its lib/build; `CHTYPES_LIB_BUILD` names the same directory for
// unsafe_families.txt discovery). A shipped binary needs an rpath relative
// to itself, or the RUNPATH points at the builder's filesystem and the
// library is unfindable in a runtime image: Linux is the shipping target,
// and cgo's flag validator accepts $ORIGIN. It rejects macOS's @loader_path
// spelling, so a relocatable darwin binary needs
// `go build -ldflags "-r @loader_path"`.
#cgo CFLAGS: -I${SRCDIR}/../../include
#cgo LDFLAGS: -lchtypes
#cgo linux LDFLAGS: -Wl,-rpath,$ORIGIN -Wl,-rpath,$ORIGIN/../lib
#include <stdlib.h>
#include "chtypes.h"
*/
import "C"

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"unsafe"
)

// ---------------------------------------------------------------- process init

var (
	initOnce sync.Once
	initErr  error
	// builtVersion is the ClickHouse release this binary was vendored from.
	// Set eagerly, because callers print it before doing any work and a lazily
	// populated version string reads as an empty one. Exposed through the
	// BuiltVersion accessor — a package VAR here was assignable by any caller,
	// and a caller that overwrote it would re-route checkVersion for the whole
	// process (fixed 2026-08-26; pre-1.0 break, sanctioned).
	builtVersion Version

	// defaultSettingsMu protects the ONE piece of process-global state the
	// statically linked path can mutate after startup.
	//
	// chs_set_default_settings REPLACES the seeded settings list wholesale, and
	// chs_row / chs_rows / chs_schema_compile read that same list BY REFERENCE
	// while they work. Replacing it under a reader is not a stale read, it is a
	// use-after-free. The C header presents the call as start-up configuration
	// ("a gateway knows them once and should not have to repeat them per row")
	// but nothing in the ABI stops a caller making it mid-flight, so the
	// exclusion is enforced here instead of documented.
	//
	// Writer: SetDefaultSettings. Readers: every entry point that reaches
	// chs_row, chs_rows, chs_schema_compile or chs_validate_type. Read locks are
	// shared, so this costs concurrent callers nothing and is NOT the
	// per-schema serialisation — CompiledSchema.mu is still what makes one
	// handle single-threaded.
	//
	// The dlopen'd multi-version path never calls chs_set_default_settings (it
	// is not in the function-pointer table), so Library needs no equivalent;
	// see the "locking" commentary in multiversion.go.
	defaultSettingsMu sync.RWMutex
)

func init() {
	// Cheap: a pointer to a string constant compiled into the library. The
	// expensive part of setup (the DateLUT, the aggregate registry, the global
	// Context) stays lazy in ensureInit.
	builtVersion = Version(C.GoString(C.chs_clickhouse_version()))
}

// BuiltVersion is the ClickHouse release this binary was vendored from — the
// version the statically linked path answers for. Available before the first
// call (it is a string constant compiled into the library, read at package
// init). An accessor rather than a package var, so no caller can overwrite
// the process's idea of what it linked.
func BuiltVersion() Version { return builtVersion }

func ensureInit() error {
	initOnce.Do(func() {
		// The statically linked library and this package come from one build,
		// so a mismatch here is not a version-skew problem — it means
		// lib/build was staged from a DIFFERENT artifact than the header cgo
		// compiled against, which is precisely the failure that produced a
		// wrong-library link before. Cheap, and checked before anything is
		// called through the mismatched declarations.
		if got := int(C.chs_abi_revision()); got != ABIRevision {
			initErr = fmt.Errorf(
				"chtypes: ABI revision mismatch: the linked library reports %d, "+
					"this package was compiled against %d (lib/build was staged "+
					"from a different artifact than chtypes.h)", got, ABIRevision)
			return
		}
		ctz := C.CString(Timezone)
		defer C.free(unsafe.Pointer(ctz))

		// The refuse-list is generated at build time by probing every family in
		// ClickHouse's runtime registry (tools/gen_unsafe_families.py). It is
		// read, not compiled in, so a rebuild against a new release picks up a
		// new Context-requiring family without a code change.
		list := ""
		if b, err := os.ReadFile(unsafeListPath()); err == nil {
			list = strings.TrimSpace(string(b))
		}
		cl := C.CString(list)
		defer C.free(unsafe.Pointer(cl))

		// Server-level type gates a gateway knows once (see
		// chs_set_default_settings). CHTYPES_SETTINGS is a JSON object.
		if ds := os.Getenv("CHTYPES_SETTINGS"); ds != "" {
			cds := C.CString(ds)
			var setErr *C.char
			rc := C.chs_set_default_settings(cds, &setErr)
			C.free(unsafe.Pointer(cds))
			if rc != 0 {
				msg := ""
				if setErr != nil {
					msg = C.GoString(setErr)
					C.chs_free(setErr)
				}
				initErr = fmt.Errorf("chtypes: chs_set_default_settings(CHTYPES_SETTINGS) failed: %d: %s", int(rc), msg)
				return
			}
		}
		var cErr *C.char
		if rc := C.chs_init(ctz, cl, &cErr); rc != 0 {
			msg := ""
			if cErr != nil {
				msg = C.GoString(cErr)
				C.chs_free(cErr)
			}
			initErr = fmt.Errorf("chtypes: chs_init failed: %d: %s", int(rc), msg)
			return
		}
		builtVersion = Version(C.GoString(C.chs_clickhouse_version()))
	})
	return initErr
}

func unsafeListPath() string {
	if p := os.Getenv("CHTYPES_UNSAFE_FAMILIES"); p != "" {
		return p
	}
	if exe, err := os.Executable(); err == nil {
		if p := filepath.Join(filepath.Dir(exe), "unsafe_families.txt"); fileExists(p) {
			return p
		}
	}
	if d := libBuildDir(); d != "" {
		return filepath.Join(d, "unsafe_families.txt")
	}
	return "unsafe_families.txt"
}

// libBuildDir is the linked native build tree — the directory CGO_LDFLAGS
// pointed -L at when this binary was built. The SDK cannot know it (the
// build tree lives in the core repository, not here), so the same caller
// that set CGO_LDFLAGS names it in CHTYPES_LIB_BUILD; empty when unset.
// Used only to find unsafe_families.txt next to the linked library.
func libBuildDir() string { return os.Getenv("CHTYPES_LIB_BUILD") }

func fileExists(p string) bool { _, err := os.Stat(p); return err == nil }

// checkVersion enforces that the caller is asking for the semantics this
// binary actually contains. "" and a prefix of the built version pass.
func checkVersion(v Version) error {
	if err := ensureInit(); err != nil {
		return err
	}
	if v == "" {
		return nil
	}
	built := string(builtVersion)
	want := string(v)
	if built == want || strings.HasPrefix(built, want+".") || strings.HasPrefix(built, want+"-") {
		return nil
	}
	return fmt.Errorf("chtypes: this build is ClickHouse %s; it cannot answer for %s "+
		"(rebuild against that tag)", built, want)
}

// ---------------------------------------------------------------- public API

// ValidateType reports whether a type expression is legal on this version,
// and returns its canonical form (chs_validate_type). Canonicalisation is
// ClickHouse's own and not a spelling normaliser: "DECIMAL(18,4)" →
// "Decimal(18, 4)", "BIGINT" → "Int64", Variant members are sorted. Compare
// the returned spelling verbatim; never re-normalise whitespace.
//
// Errors: a *SchemaError when ClickHouse itself refuses the expression
// (Code is the server's own, e.g. 50 "Unknown data type family"); an
// *UnsupportedError when this build declines to answer. A version this
// binary was not built from is a plain error (see Version).
//
// ValidateType alone cannot see DEFAULT-driven type rewrites ("x Int64
// DEFAULT NULL" compiles as Nullable(Int64)) — use CompileDDL for
// schema-aware answers.
func ValidateType(v Version, typeExpr string) (canonical string, err error) {
	if err := checkVersion(v); err != nil {
		return "", err
	}
	cexpr := C.CString(typeExpr)
	defer C.free(unsafe.Pointer(cexpr))

	var cCanon, cErr *C.char
	var code C.int
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	if rc := C.chs_validate_type(cexpr, &cCanon, &code, &cErr); rc != 0 {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_free(cErr)
		}
		// The RETURN VALUE carries the code (spec/c-abi.md: every out param
		// is optional); out_code is the convenience copy. Prefer whichever is
		// nonzero, so the guarded-exception corner (rc=-1 with out_code=0)
		// still funnels as the decline it is rather than "a refusal, code 0".
		effective := int(code)
		if effective == 0 {
			effective = int(rc)
		}
		return "", schemaErr(effective, msg, "")
	}
	canonical = C.GoString(cCanon)
	C.chs_free(cCanon)
	return canonical, nil
}

// CompiledSchema is a schema bound to the vendored ClickHouse build — the
// statically linked twin of LoadedSchema. Columns/Canonical/LiteralDefault
// mirror the C column-introspection group in ClickHouse's own canonical
// spelling.
//
// A CompiledSchema is single-threaded: one handle must not be used from two
// goroutines at once (an internal mutex enforces it); DISTINCT schemas
// proceed in parallel. Close releases the native handle; a finalizer covers
// forgetting it.
type CompiledSchema struct {
	Version Version
	Columns []Column
	// Canonical holds each column's canonical type, positionally.
	Canonical []string
	// LiteralDefault reports, positionally, whether a column's DEFAULT can be
	// applied without the expression interpreter.
	LiteralDefault []bool

	handle *C.chs_schema
	mu     sync.Mutex
	// filters tracks every open Filter compiled from this handle, so Close
	// can free them FIRST — the C layer does not refcount, and freeing the
	// schema under a live filter is use-after-free (spec/c-abi.md §Filters,
	// handle lifetime). Guarded by mu.
	filters map[*Filter]struct{}
	// blocks tracks every open Block parsed from this handle — the same
	// non-owning rule, the same free-before-schema order (spec/c-abi.md
	// §Blocks). Guarded by mu.
	blocks map[*Block]struct{}
}

// ParseSchema validates every type expression and DEFAULT expression in s,
// rebuilding it as DDL text and compiling through CompileDDL — a convenience
// for callers that hold a []Column rather than DDL text.
//
// Errors are CompileDDL's: a *SchemaError (ClickHouse refused; Code is the
// server's own, and Column names the offending column when attributable) or
// an *UnsupportedError (this build declines — e.g. a DEFAULT past the
// admission budget).
func ParseSchema(v Version, s Schema) (*CompiledSchema, error) {
	if err := checkVersion(v); err != nil {
		return nil, err
	}

	// Rebuild the column list as ClickHouse DDL and hand it to ClickHouse's own
	// ParserColumnDeclarationList, so DEFAULT expressions are validated as real
	// SQL rather than by a parser of ours.
	var b strings.Builder
	for i, c := range s.Columns {
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString(QuoteIdentifier(c.Name))
		b.WriteByte(' ')
		b.WriteString(c.Type)
		if k := c.DefaultKind.String(); k != "" && c.Default != "" {
			b.WriteByte(' ')
			b.WriteString(k)
			b.WriteByte(' ')
			b.WriteString(c.Default)
		}
	}
	return CompileDDL(v, b.String())
}

// CompileDDL compiles a raw ClickHouse column-declaration list — "a UInt8,
// b Nullable(String) DEFAULT 'x'", NOT a CREATE TABLE — through ClickHouse's
// own ParserColumnDeclarationList (chs_schema_compile), optionally under a
// DECLARED settings profile (WithCompileSettings) and compile mode
// (WithCompileMode). Column-level TTL clauses belong in the DDL and are
// captured here; the table-level rows TTL is SetTTL. ParseSchema is a thin
// wrapper over it.
//
// With no options, settings are NULL/"{}" and mode is CompileDeclared — the
// same compile-base path this call has always taken, structurally: the C
// side never even copies a Context for an empty profile.
//
// Errors: a *SchemaError when ClickHouse refuses the DDL (Code is the
// server's own — including 115 for an unknown setting name in the profile,
// where nothing is compiled; type-gate codes 455/44 when the profile
// declares a gate at a refusing value; the schema-level Enum-DEFAULT
// poisoning refusal, code 691). An *UnsupportedError when this build
// declines — a DEFAULT past the admission budget
// (chtypes_default_eval_memory_bytes / _wall_nanos), or a mode other than
// CompileDeclared. Callers distinguish the two with errors.As; the decline
// never satisfies errors.As(&SchemaError{}).
func CompileDDL(v Version, ddl string, opts ...CompileOption) (*CompiledSchema, error) {
	if err := checkVersion(v); err != nil {
		return nil, err
	}
	var cfg compileConfig
	for _, opt := range opts {
		opt(&cfg)
	}
	cddl := C.CString(ddl)
	defer C.free(unsafe.Pointer(cddl))
	csj := C.CString(settingsJSON(cfg.settings))
	defer C.free(unsafe.Pointer(csj))

	var code C.int
	var cErr *C.char
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	h := C.chs_schema_compile(cddl, csj, C.int(cfg.mode), &code, &cErr)
	if h == nil {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_free(cErr)
		}
		// No column is attributed, deliberately (spec/bindings.md rule 12):
		// chs_schema_compile's structured answer is a code and a message,
		// nothing more, and the longest-declared-name-in-the-message guess
		// this call used to make added no information (the library's own
		// message already names the column when it knows one) while making
		// this the only path in any SDK that attributed one. The TS binding's
		// measured removal (2026-08-26) is the precedent.
		return nil, schemaErr(int(code), msg, "")
	}

	cs := &CompiledSchema{Version: v, handle: h}
	n := int(C.chs_schema_column_count(h))
	for i := 0; i < n; i++ {
		ci := C.int(i)
		cs.Columns = append(cs.Columns, Column{
			Name:        C.GoString(C.chs_schema_column_name(h, ci)),
			Type:        C.GoString(C.chs_schema_column_type(h, ci)),
			Default:     C.GoString(C.chs_schema_column_default_expr(h, ci)),
			DefaultKind: parseDefaultKind(C.GoString(C.chs_schema_column_default_kind(h, ci))),
		})
		cs.Canonical = append(cs.Canonical, cs.Columns[i].Type)
		cs.LiteralDefault = append(cs.LiteralDefault, C.chs_schema_column_default_is_literal(h, ci) != 0)
	}
	runtime.SetFinalizer(cs, func(x *CompiledSchema) { x.Close() })
	return cs, nil
}

// SetEngine declares the table's engine and sorting key, so Rows can apply
// the engine's own insert-time semantics — the single-block merge every
// INSERT runs under the server's default optimize_on_insert=1
// (MergeTreeDataWriter::mergeBlock): CollapsingMergeTree refusing an invalid
// Sign with code 117 before anything is stored, SummingMergeTree summing
// equal keys and dropping all-zero rows, ReplacingMergeTree deduplicating
// within the block. engine is the SHOW CREATE spelling
// ("CollapsingMergeTree(sign)"); orderBy the sorting key ("tuple()", "id",
// "(day, key)"). "" or "MergeTree" is a no-op. An engine this build does not
// model returns an *UnsupportedError — never a guess.
//
// WithMergeTreeSettings adds the table's MergeTree-namespace settings; with
// no options this is identical to declaring none.
func (cs *CompiledSchema) SetEngine(engine, orderBy string, opts ...EngineOption) error {
	var cfg engineConfig
	for _, opt := range opts {
		opt(&cfg)
	}
	// Shared: chs_schema_engine reads the seeded settings; only
	// SetDefaultSettings writes them. cs.mu is what serialises this handle.
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.handle == nil {
		return fmt.Errorf("chtypes: schema is closed")
	}
	ce := C.CString(engine)
	defer C.free(unsafe.Pointer(ce))
	co := C.CString(orderBy)
	defer C.free(unsafe.Pointer(co))
	cmt := C.CString(settingsJSON(cfg.mergeTreeSettings))
	defer C.free(unsafe.Pointer(cmt))
	var cErr *C.char
	rc := C.chs_schema_engine(cs.handle, ce, co, cmt, &cErr)
	if rc == 0 {
		return nil
	}
	msg := ""
	if cErr != nil {
		msg = C.GoString(cErr)
		C.chs_free(cErr)
	}
	// The sign of rc is the whole rule (spec/bindings.md, spec/c-abi.md
	// "Error model"). A POSITIVE rc is a real ClickHouse error code: the
	// server's own engine validation REFUSED this DDL, so the table can never
	// exist and no data and no retry will change that — a tenant has to be
	// told. It surfaces as the same *SchemaError a compile rejection does,
	// carrying the server's code and the server's message (which is where the
	// "Maybe you meant ..." hint lives). A NEGATIVE rc is this library
	// DECLINING — -2 "I will not guess", -1 a guarded exception — and becomes
	// an *UnsupportedError, which a caller must validate cautiously rather than
	// report as the tenant's fault. schemaErr keys on the sign rather than on
	// == 115, so a code this era does not yet return cannot silently become a
	// decline.
	return schemaErr(int(rc), msg, "")
}

// SetTTL declares the table's rows TTL (the `TTL ...` clause after the
// engine, e.g. "ts + INTERVAL 30 DAY"). Rows then applies ClickHouse's own
// TTLDeleteAlgorithm at preview time the way OPTIMIZE FINAL applies it at
// merge: an expired row is reported not stored, with a Transformed entry
// (ReasonTTLExpired) naming it. Column-level TTLs need no call — they are in
// the DDL and captured at CompileDDL (ReasonTTLColumnExpired on reset).
// "" is a no-op. WHERE/GROUP BY TTLs, moves, and clock-reading expressions
// return an *UnsupportedError — never a guess.
func (cs *CompiledSchema) SetTTL(ttl string) error {
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.handle == nil {
		return fmt.Errorf("chtypes: schema is closed")
	}
	ct := C.CString(ttl)
	defer C.free(unsafe.Pointer(ct))
	var cErr *C.char
	rc := C.chs_schema_ttl(cs.handle, ct, &cErr)
	if rc == 0 {
		return nil
	}
	msg := ""
	if cErr != nil {
		msg = C.GoString(cErr)
		C.chs_free(cErr)
	}
	return &UnsupportedError{Msg: msg}
}

// Close releases the native schema. Optional: a finalizer does it too. Any
// Filter or Block still open on this schema is closed FIRST, in the same
// call — the handles-before-schema free order the C layer requires, enforced
// here so no caller ordering (and no finalizer timing) can get it backwards.
func (cs *CompiledSchema) Close() {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	for f := range cs.filters {
		f.closeLocked()
	}
	cs.filters = nil
	for b := range cs.blocks {
		b.closeLocked()
	}
	cs.blocks = nil
	if cs.handle != nil {
		C.chs_schema_free(cs.handle)
		cs.handle = nil
	}
}

// Filter is one boolean SQL expression compiled against a CompiledSchema's
// columns (chs_filter_compile) — the same TreeRewriter + ExpressionAnalyzer
// pipeline the CONSTRAINT CHECK path runs, so comparison semantics are
// WHERE-side by construction: `x = 256` over UInt8 promotes (false for every
// row), it never wraps.
//
// LIFETIME: a Filter references its schema handle; the C layer does not
// refcount (spec/c-abi.md §Filters). This binding enforces the free order
// structurally, both ways: the Filter holds its *CompiledSchema (so the
// schema finalizer cannot run first — Go runs finalizers in dependency
// order), and CompiledSchema.Close closes every open Filter before freeing
// the schema. Close a Filter when done; a finalizer covers forgetting it.
// A filter compiled from a schema handle answers for THAT handle: recompile
// filters when the schema is recompiled.
//
// THREADS: one Filter must not be used from two threads at once, and a
// Filter.Rows call is ALSO a use of the filter's schema handle — two filters
// over ONE schema must not run concurrently either (the header's rule,
// verbatim). This binding enforces both by taking the schema's own handle
// lock for every filter call; distinct schemas remain fully concurrent.
//
// ENFORCEMENT GATE: nothing may enforce read-side security on this surface
// until the WHERE-truth rig gates green (zero over-admit, zero over-hide);
// until then it is a shadow/replay surface (spec/c-abi.md §Filters).
type Filter struct {
	// Expr is the expression text as compiled, for logging and cache keys.
	Expr string

	cs     *CompiledSchema
	handle *C.chs_filter
}

// CompileFilter compiles one boolean expression over this schema's PHYSICAL
// columns (ordinary + MATERIALIZED; naming an ALIAS/EPHEMERAL column fails
// with ClickHouse's own UNKNOWN_IDENTIFIER, exactly where a real CREATE
// fails). The expression may contain `{name:Type}` query parameters, bound
// with WithFilterParams — substitution is the server's own
// ReplaceQueryParameterVisitor, run before analysis, exactly where a real
// server runs it.
//
// Errors follow rule 12's split: a *SchemaError when ClickHouse itself
// refuses the expression (unknown identifier 47, unknown function, a
// NO_COMMON_TYPE the analyzer raises — and, since ABI revision 4, the
// server's own parameter refusals: an UNBOUND `{name:Type}` is 456
// UNKNOWN_QUERY_PARAMETER ("Substitution `name` is not set"), a value the
// declared type cannot parse completely is 457 BAD_QUERY_PARAMETER — the
// server's own code and message, verbatim); an *UnsupportedError when this
// build declines — a non-deterministic expression (clock reads:
// `now() > ts`; rand(); server-constants; the scan runs AFTER substitution,
// so a parameter value can never smuggle one in). A bound name the
// expression never uses is ignored, as a live server ignores an unused
// param_*.
func (cs *CompiledSchema) CompileFilter(expr string, opts ...FilterOption) (*Filter, error) {
	var cfg filterConfig
	for _, opt := range opts {
		opt(&cfg)
	}
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.handle == nil {
		return nil, fmt.Errorf("chtypes: schema is closed")
	}
	cexpr := C.CString(expr)
	defer C.free(unsafe.Pointer(cexpr))
	cparams := C.CString(settingsJSON(cfg.params))
	defer C.free(unsafe.Pointer(cparams))
	var code C.int
	var cErr *C.char
	h := C.chs_filter_compile(cs.handle, cexpr, cparams, &code, &cErr)
	if h == nil {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_free(cErr)
		}
		return nil, schemaErr(int(code), msg, "")
	}
	f := &Filter{Expr: expr, cs: cs, handle: h}
	if cs.filters == nil {
		cs.filters = map[*Filter]struct{}{}
	}
	cs.filters[f] = struct{}{}
	runtime.SetFinalizer(f, func(x *Filter) { x.Close() })
	return f, nil
}

// Rows evaluates the filter over a body of rows (chs_filter_rows) — same
// formats and settings contract as CompiledSchema.Rows, one C call. Rows are
// evaluated INDEPENDENTLY (there is no INSERT to abort):
// input_format_allow_errors_* does not apply, a bad text row declines ('d')
// and the tail resyncs so verdict indexes keep matching input rows, and
// volatile DEFAULTs resolve against one clock instant per call. The
// call-level verdict is FilterResult.Outcome; the error return is only for a
// closed filter or an unreadable document.
func (f *Filter) Rows(format Format, body []byte, settings map[string]string) (FilterResult, error) {
	cs := f.cs
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	// The filter call IS a use of its schema handle: take the schema's own
	// lock, which is also what makes two filters over one schema never run
	// concurrently.
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if f.handle == nil {
		return FilterResult{}, fmt.Errorf("chtypes: filter is closed")
	}

	sj := settingsJSON(settings)
	csj := C.CString(sj)
	defer C.free(unsafe.Pointer(csj))

	var pbody *C.char
	if len(body) > 0 {
		pbody = (*C.char)(unsafe.Pointer(&body[0]))
	} else {
		pbody = C.CString("")
		defer C.free(unsafe.Pointer(pbody))
	}

	out := C.chs_filter_rows(f.handle, C.int(format), pbody, C.size_t(len(body)), csj)
	runtime.KeepAlive(body)
	if out == nil {
		return FilterResult{}, fmt.Errorf("chtypes: chs_filter_rows returned null")
	}
	js := C.GoString(out)
	C.chs_free(out)
	return filterResultOf(js)
}

// Close releases the native filter. Idempotent; safe before OR via the
// schema's own Close (which closes open filters first); a finalizer covers
// forgetting it entirely.
func (f *Filter) Close() {
	cs := f.cs
	if cs == nil {
		return
	}
	cs.mu.Lock()
	defer cs.mu.Unlock()
	f.closeLocked()
}

// closeLocked frees the filter handle. Caller holds cs.mu.
func (f *Filter) closeLocked() {
	if f.handle != nil {
		C.chs_filter_free(f.handle)
		f.handle = nil
	}
	if f.cs != nil && f.cs.filters != nil {
		delete(f.cs.filters, f)
	}
}

// ---------------------------------------------------------------- blocks

// Block is one body, parsed ONCE under one schema handle and one clock
// instant (chs_block_parse) — the parse half of Filter.Rows, exported so K
// filters can evaluate one event without re-parsing it (the live-SSE call
// shape; spec/c-abi.md §Blocks). Per-row parse failures are recorded IN the
// block (those rows answer VerdictDecline with the recorded error from every
// filter); a call-level failure (unknown setting 115, an unsplittable body, a
// binary decode fault, the deferred JSONEachRow framing verdict) fails
// ParseBlock instead — a malformed body yields no Block and no partial
// answers.
//
// LIFETIME: a Block references its schema handle exactly as a Filter does —
// no copy, no refcount. This binding enforces the order both ways: the Block
// holds its *CompiledSchema (finalizers run in dependency order) and
// CompiledSchema.Close closes every open Block before freeing the schema. A
// Block may be evaluated by MANY filters, sequentially; evaluation does not
// consume or mutate it. Recompile blocks when the schema is recompiled.
//
// THREADS: one Block must not be used from two threads at once, and an Eval
// call is a use of BOTH handles — this binding takes the schema's own handle
// lock for every block call, which also serialises K filters over one block.
type Block struct {
	cs     *CompiledSchema
	handle *C.chs_block
}

// ParseBlock parses a body once under this schema — same formats and settings
// contract as Rows (settings are the PARSE-side map: format settings, clock
// keys; evaluation takes none). Volatile DEFAULTs resolve against THIS call's
// clock instant, so eval(parse(body)) ≡ Filter.Rows(body) exactly when the
// clock is pinned (chtypes_now_epoch_nanos) or no volatile DEFAULT exists.
//
// The error return carries the call-level refusal (a *SchemaError with
// ClickHouse's own code — an unknown setting's 115, a framing or decode
// fault — or an *UnsupportedError for a decline): no partial block exists on
// any error.
func (cs *CompiledSchema) ParseBlock(format Format, body []byte, settings map[string]string) (*Block, error) {
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.handle == nil {
		return nil, fmt.Errorf("chtypes: schema is closed")
	}

	csj := C.CString(settingsJSON(settings))
	defer C.free(unsafe.Pointer(csj))
	var pbody *C.char
	if len(body) > 0 {
		pbody = (*C.char)(unsafe.Pointer(&body[0]))
	} else {
		pbody = C.CString("")
		defer C.free(unsafe.Pointer(pbody))
	}

	var code C.int
	var cErr *C.char
	h := C.chs_block_parse(cs.handle, C.int(format), pbody, C.size_t(len(body)), csj, &code, &cErr)
	runtime.KeepAlive(body)
	if h == nil {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_free(cErr)
		}
		return nil, schemaErr(int(code), msg, "")
	}
	b := &Block{cs: cs, handle: h}
	if cs.blocks == nil {
		cs.blocks = map[*Block]struct{}{}
	}
	cs.blocks[b] = struct{}{}
	runtime.SetFinalizer(b, func(x *Block) { x.Close() })
	return b, nil
}

// Eval evaluates this filter over an already-parsed Block (chs_filter_eval)
// and returns the SAME result document Filter.Rows returns — same
// FilterResult fields, same verdict characters, same Errors rule (a row the
// parse recorded as unparseable answers VerdictDecline with the recorded
// error). Evaluation is a pure function of (filter, block): it takes no
// settings and does not mutate the block, so one Block can be evaluated by K
// filters sequentially with no re-parse.
//
// Filter and Block MUST come from the SAME schema handle: a mismatched pair
// answers a rejected document (code 1002) — the C layer's loud refusal,
// never undefined behaviour. The error return is only for a closed filter or
// block, or an unreadable document.
func (f *Filter) Eval(b *Block) (FilterResult, error) {
	cs := f.cs
	if b.cs != cs {
		// Different schema handles. Same-library pairs go to C, which answers
		// the contract's rejected 1002; both handles are locked in a fixed
		// global order so two crossed Evals cannot deadlock.
		return f.evalCrossSchema(b)
	}
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	return f.evalLocked(b)
}

// evalCrossSchema handles the (filter, block) pair from two DIFFERENT schema
// handles: both schema locks are taken (in pointer order, so two crossed
// Evals cannot deadlock) and the C layer answers its rejected-1002 document.
func (f *Filter) evalCrossSchema(b *Block) (FilterResult, error) {
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	first, second := f.cs, b.cs
	if uintptr(unsafe.Pointer(first)) > uintptr(unsafe.Pointer(second)) {
		first, second = second, first
	}
	first.mu.Lock()
	defer first.mu.Unlock()
	second.mu.Lock()
	defer second.mu.Unlock()
	return f.evalLocked(b)
}

// evalLocked runs chs_filter_eval. Caller holds the schema lock(s) covering
// both handles and the defaultSettings read lock.
func (f *Filter) evalLocked(b *Block) (FilterResult, error) {
	if f.handle == nil {
		return FilterResult{}, fmt.Errorf("chtypes: filter is closed")
	}
	if b.handle == nil {
		return FilterResult{}, fmt.Errorf("chtypes: block is closed")
	}
	out := C.chs_filter_eval(f.handle, b.handle)
	if out == nil {
		return FilterResult{}, fmt.Errorf("chtypes: chs_filter_eval returned null")
	}
	js := C.GoString(out)
	C.chs_free(out)
	return filterResultOf(js)
}

// Close releases the native block. Idempotent; safe before OR via the
// schema's own Close (which closes open blocks first); a finalizer covers
// forgetting it entirely.
func (b *Block) Close() {
	cs := b.cs
	if cs == nil {
		return
	}
	cs.mu.Lock()
	defer cs.mu.Unlock()
	b.closeLocked()
}

// closeLocked frees the block handle. Caller holds cs.mu.
func (b *Block) closeLocked() {
	if b.handle != nil {
		C.chs_block_free(b.handle)
		b.handle = nil
	}
	if b.cs != nil && b.cs.blocks != nil {
		delete(b.cs.blocks, b)
	}
}

// Rows validates and coerces a whole request body against a compiled schema
// (chs_rows). body is raw bytes in the given Format — binary formats contain
// NUL bytes and text rows can contain invalid UTF-8, so it is never a
// string. settings is the per-call ClickHouse settings map (nil is fine);
// values must be strings and win over the handle's compile profile, which
// wins over SetDefaultSettings, which wins over ClickHouse's defaults —
// except a type gate the compile profile declared, which the handle owns.
//
// Rows is NOT Row in a loop: row separation is format-specific, and
// input_format_allow_errors_num/_ratio decide whether a bad row is skipped
// or aborts the batch. One batch is one clock instant for volatile DEFAULTs.
//
// The verdicts live in the BatchResult (Outcome per batch and per row,
// EngineRows as the stored truth when present, storage TTL effects folded
// into Transformed); the error return is only for a closed schema or an
// unreadable result document, never a ClickHouse verdict.
func (cs *CompiledSchema) Rows(format Format, body []byte, settings map[string]string) (BatchResult, error) {
	// export off, all document groups on: the revision-3 pass-through that
	// keeps Rows() byte-identical to revision 2 (spec/bindings.md §Revision 3).
	return cs.rowsThrough(format, body, settings, ExportNone, DocAll)
}

// RowsExport is Rows with the revision-3 export and document-flag channels
// exposed: ONE chs_rows call, never a second, never re-parsing
// (docs/proposals/rows-export.md; spec/c-abi.md §Rows is normative).
//
// exportFormat is ExportNone (no bytes; the docFlags still thin the
// document) or a Format this artifact can SERIALIZE — this revision exactly
// JSONCompactEachRow. Any other value answers the whole call
// Outcome == Unsupported and processes nothing — loud, never silent.
//
// docFlags select the document groups (OR'd together); none means 0, the
// LEAN document: verdicts intact, but Values, Transformed, Substituted,
// Computed and UnknownFields all come back empty — Rows() is the DocAll
// spelling. Under DocTransforms without DocValues, Transformed is still
// derived exactly as always (the C layer retains every entry a detector
// could fire on); under DocValues without DocTransforms the reference parse
// is skipped C-side and Transformed can only carry what detector 1 sees.
//
// The exported bytes come back in BatchResult.Payload with
// BatchResult.Spans index-aligned to Rows — Payload[s.Off:s.Off+s.Len] IS
// row i's line — and the emitted-empty versus declined distinction is
// Payload non-nil-empty versus nil + ExportDeclined (see BatchResult). The
// C buffer is copied and freed (same library's chs_free) before this
// returns; no ownership crosses the cgo boundary.
func (cs *CompiledSchema) RowsExport(format Format, body []byte, settings map[string]string, exportFormat Format, docFlags ...DocFlags) (BatchResult, error) {
	var flags DocFlags
	for _, f := range docFlags {
		flags |= f
	}
	return cs.rowsThrough(format, body, settings, exportFormat, flags)
}

// rowsThrough is the ONE chs_rows call site on the statically linked path —
// Rows and RowsExport are both single invocations of it with different
// parameters, exactly as the proposal requires.
func (cs *CompiledSchema) rowsThrough(format Format, body []byte, settings map[string]string, exportFormat Format, flags DocFlags) (BatchResult, error) {
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.handle == nil {
		return BatchResult{}, fmt.Errorf("chtypes: schema is closed")
	}

	sj := settingsJSON(settings)
	csj := C.CString(sj)
	defer C.free(unsafe.Pointer(csj))

	var pbody *C.char
	if len(body) > 0 {
		pbody = (*C.char)(unsafe.Pointer(&body[0]))
	} else {
		pbody = C.CString("")
		defer C.free(unsafe.Pointer(pbody))
	}

	// The export buffer rides only when an export is requested; Rows()
	// passes NULL, which is what keeps its document byte-identical to
	// revision 2 (the C side treats a non-exporting NULL as the old path).
	var ob *C.chs_bytes
	var obv C.chs_bytes
	if exportFormat != ExportNone {
		ob = &obv
	}
	out := C.chs_rows(cs.handle, C.int(format), pbody, C.size_t(len(body)), csj,
		C.int(exportFormat), C.uint(flags), ob)
	runtime.KeepAlive(body)
	// Copy-then-free the export buffer FIRST, whatever happens to the
	// document: the bytes are library-owned malloc'd memory and this is the
	// one place that sees the pointer.
	var payload []byte
	if ob != nil && obv.data != nil {
		if obv.len > 0 {
			payload = C.GoBytes(unsafe.Pointer(obv.data), C.int(obv.len))
		} else {
			// Emitted-empty: data non-NULL, len 0 — an accepted batch with
			// zero accepted rows. Distinguishable from nil (declined).
			payload = []byte{}
		}
		C.chs_free(obv.data)
	}
	if out == nil {
		return BatchResult{}, fmt.Errorf("chtypes: chs_rows returned null")
	}
	js := C.GoString(out)
	C.chs_free(out)

	res, err := batchResultOf(js)
	if err != nil {
		return res, err
	}
	res.Payload = payload
	return res, nil
}

// Row validates and coerces one input row against a compiled schema
// (chs_row). raw is the row's bytes in the given Format.
//
// It applies literal DEFAULTs for absent columns, enforces nullability, coerces
// each value exactly as ClickHouse would (or refuses), and reports every value
// it silently transformed. The verdict is RowResult.Outcome — Accepted,
// Rejected (ErrCode = the server's own code), AcceptedPoisoned or
// Unsupported; the error return is only for a closed schema or an unreadable
// result document. For a multi-row body use Rows, which is not this in a
// loop.
func (cs *CompiledSchema) Row(format Format, raw []byte) (RowResult, error) {
	return cs.RowWithSettings(format, raw, nil)
}

// RowWithSettings is Row with per-call ClickHouse settings applied. Values
// must be strings (see SetDefaultSettings); an unknown setting name rejects
// the call with the server's own code 115 in the RowResult.
func (cs *CompiledSchema) RowWithSettings(format Format, raw []byte, settings map[string]string) (RowResult, error) {
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.handle == nil {
		return RowResult{}, fmt.Errorf("chtypes: schema is closed")
	}

	sj := settingsJSON(settings)
	csj := C.CString(sj)
	defer C.free(unsafe.Pointer(csj))

	var praw *C.char
	if len(raw) > 0 {
		praw = (*C.char)(unsafe.Pointer(&raw[0]))
	} else {
		praw = C.CString("")
		defer C.free(unsafe.Pointer(praw))
	}

	out := C.chs_row(cs.handle, C.int(format), praw, C.size_t(len(raw)), csj)
	runtime.KeepAlive(raw)
	if out == nil {
		return RowResult{}, fmt.Errorf("chtypes: chs_row returned null")
	}
	js := C.GoString(out)
	C.chs_free(out)

	var doc rowDoc
	if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
		return RowResult{}, fmt.Errorf("chtypes: bad result document: %w", err)
	}
	res := rowResultOf(doc)
	return res, nil
}

// ---------------------------------------------------------------- helpers

// SetDefaultSettings seeds the settings every later call starts from
// (chs_set_default_settings) — the "library defaults" tier of the
// precedence chain: per-call > compile profile > these > ClickHouse's own.
// The column-creation gates ClickHouse applies (allow_suspicious_low_cardinality_types,
// allow_experimental_time_time64_type, …) are properties of the server the
// table lives on, not of the row, so a gateway sets them once. Per-call
// settings still win.
//
// Values must be strings: a 19-digit nanosecond epoch does not survive an
// IEEE double, and a numeric value would be silently ignored. An unknown
// setting name refuses the WHOLE payload with the server's own code 115 —
// nothing is committed, so a caller can never believe a default profile is
// in force when part of it never applied. The returned error wraps that code
// and the server's message (with its did-you-mean hint). This is also the
// only channel for the process-wide admission budgets
// (chtypes_default_eval_memory_bytes, chtypes_default_eval_wall_nanos,
// chtypes_custom_settings_prefixes).
//
// Safe to call concurrently: it takes the exclusive side of the lock every
// other call holds shared, so it never swaps settings under a running call.
// It affects only the statically linked path — a dlopen'd Library
// structurally cannot make this call.
//
// Prefer CompileDDL with WithCompileSettings for a gate that belongs to a
// particular tenant's table: a gate declared in the compile profile binds
// where a real server binds it — once, at CREATE — and then outranks the
// per-call map for that handle, so an insert that does not repeat the gate
// is not refused (measured on live 25.10.7.6 and 26.7.3.19; spec/c-abi.md,
// "Server-level type gates"). This process-wide seed stays the right channel
// only for gateway-uniform policy.
func SetDefaultSettings(settings map[string]string) error {
	if err := ensureInit(); err != nil {
		return err
	}
	b, _ := json.Marshal(settings)
	c := C.CString(string(b))
	defer C.free(unsafe.Pointer(c))
	// The one writer against the seeded settings list. Exclusive, so no call
	// can be inside chs_row/chs_rows/chs_schema_compile reading it.
	defaultSettingsMu.Lock()
	defer defaultSettingsMu.Unlock()
	var cErr *C.char
	if rc := C.chs_set_default_settings(c, &cErr); rc != 0 {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_free(cErr)
		}
		return fmt.Errorf("chtypes: chs_set_default_settings failed: %d: %s", int(rc), msg)
	}
	return nil
}

// RegisteredFamilies lists every type family in ClickHouse's runtime registry
// (chs_registered_families) — 139 entries on the 25.8 artifact. There is no
// table to maintain: rebasing onto a new release picks up new families
// automatically. The only error is a failed library initialisation.
//
// One of the three-question introspection surface every SDK exposes
// (spec/bindings.md §Introspection); the dlopen'd path's twin is
// (*Library).RegisteredFamilies.
func RegisteredFamilies() ([]string, error) {
	if err := ensureInit(); err != nil {
		return nil, err
	}
	c := C.chs_registered_families()
	s := C.GoString(c)
	C.chs_free(c)
	var out []string
	for _, l := range strings.Split(s, "\n") {
		if l != "" {
			out = append(out, l)
		}
	}
	return out, nil
}

// FunctionFlags returns the TSV audit of every registered function's
// volatility (chs_function_flags), VERBATIM: one function per line, six
// tab-separated fields — name, deterministic, deterministic_in_query,
// server_constant, stateful, resolver_error_code. These are ClickHouse's own
// answers off this build's own registry, and they are the input to the
// statelessness gate (lib/tools/gen_function_flags.py fails the build unless
// the admitted volatile set is exactly the four clock reads).
//
// One of the three-question introspection surface every SDK exposes
// (spec/bindings.md §Introspection); the dlopen'd path's twin is
// (*Library).FunctionFlags. The only error is a failed library
// initialisation.
func FunctionFlags() (string, error) {
	if err := ensureInit(); err != nil {
		return "", err
	}
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	c := C.chs_function_flags()
	s := C.GoString(c)
	C.chs_free(c)
	return s, nil
}

// ReferenceType returns the widened reference type this build compares a type
// against (chs_reference_type) — UInt8 → Int256, DateTime → DateTime64(0,
// 'UTC'), UUID → String — and "" for a type with no wider type to compare
// against (String, Float64). Diagnostic: the reference parse already happens
// inside Row/Rows and is reported per column; exposing it makes a
// transformation finding explainable.
//
// One of the three-question introspection surface every SDK exposes
// (spec/bindings.md §Introspection); the dlopen'd path's twin is
// (*Library).ReferenceType. The only error is a failed library
// initialisation.
func ReferenceType(typeExpr string) (string, error) {
	if err := ensureInit(); err != nil {
		return "", err
	}
	cexpr := C.CString(typeExpr)
	defer C.free(unsafe.Pointer(cexpr))
	defaultSettingsMu.RLock()
	defer defaultSettingsMu.RUnlock()
	c := C.chs_reference_type(cexpr)
	s := C.GoString(c)
	C.chs_free(c)
	return s, nil
}
