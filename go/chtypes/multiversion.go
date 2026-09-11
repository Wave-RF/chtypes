package chtypes

// Runtime multi-version dispatch.
//
// chtypes vendors exactly one ClickHouse tree per shared object, which made
// "answers for one version, declines four" look structural. It is not: the
// artifact is self-contained by construction — it exports only `chs_*`, keeps
// its own libc++ statically inside, and links nothing but libc and (on macOS)
// CoreFoundation — so N of them can be loaded into one process at once and
// dispatched by version at runtime.
//
// This loads each version's library with dlopen and resolves the C API through
// function pointers, so one gateway process can answer for every ClickHouse
// release it has an artifact for. What it costs is address space (~120 MB
// resident per version) and a build per tag; what it removes is the "one
// process, one version" constraint entirely.
//
// The build-time path (package-level ValidateType/ParseSchema, linked against
// one library) is unchanged and remains the fast path.

/*
#cgo LDFLAGS: -ldl
#include <dlfcn.h>
#include <stdlib.h>
#include <stddef.h>

// The C API, re-declared as function pointers so a dlopen'd library of any
// vendored version can be called without linking against it.
typedef const char * (*fn_version)(void);
typedef int          (*fn_init)(const char *, const char *, char **);
typedef void         (*fn_free)(char *);
typedef int          (*fn_validate)(const char *, char **, int *, char **);
typedef void *       (*fn_compile)(const char *, const char *, int, int *, char **);
typedef void         (*fn_schema_free)(void *);
// Revision 3: chs_rows carries export_format / doc_flags / out_bytes. The
// out-param struct is re-declared here (this file does not include
// chtypes.h) with the identical layout; the revision gate below is what
// guarantees the layouts agree before any call is made.
typedef struct { char * data; size_t len; } chs_lib_bytes;
typedef char *       (*fn_rows)(const void *, int, const char *, size_t, const char *, int, unsigned, chs_lib_bytes *);
// Revision 3: the filter trio (optional symbols — a revision-0 artifact may
// predate them; absence degrades to unsupported at call time).
// Revision 4: chs_filter_compile carries params_json ({name:Type} query
// parameters, substituted by the vendored ReplaceQueryParameterVisitor), and
// the block twin joins: chs_block_parse / chs_block_free / chs_filter_eval
// (parse a body once, evaluate K filters against the block).
typedef void *       (*fn_filter_compile)(const void *, const char *, const char *, int *, char **);
typedef void         (*fn_filter_free)(void *);
typedef char *       (*fn_filter_rows)(const void *, int, const char *, size_t, const char *);
typedef void *       (*fn_block_parse)(const void *, int, const char *, size_t, const char *, int *, char **);
typedef void         (*fn_block_free)(void *);
typedef char *       (*fn_filter_eval)(const void *, const void *);
typedef char *       (*fn_row)(const void *, int, const char *, size_t, const char *);
typedef int          (*fn_engine)(void *, const char *, const char *, const char *, char **);
typedef int          (*fn_ttl)(void *, const char *, char **);
typedef int          (*fn_col_count)(const void *);
typedef const char * (*fn_col_str)(const void *, int);
typedef int          (*fn_col_int)(const void *, int);
typedef int         (*fn_abi_rev)(void);
typedef char *       (*fn_owned_str0)(void);          // chs_registered_families, chs_function_flags
typedef char *       (*fn_owned_str1)(const char *);  // chs_reference_type

typedef struct {
    void *handle;
    fn_version      version;
    fn_init         init;
    fn_free         freep;
    fn_validate     validate;
    fn_compile      compile;
    fn_schema_free  schema_free;
    fn_rows         rows;
    fn_row          row;      // optional: absent from artifacts built before it
    fn_engine       engine;   // optional: absent from artifacts built before it
    fn_ttl          ttl;      // optional, same rule
    fn_col_count    col_count;        // optional; column introspection group
    fn_col_str      col_name;
    fn_col_str      col_type;
    fn_col_str      col_default_expr;
    fn_col_str      col_default_kind;
    fn_col_int      col_default_is_literal;
    fn_abi_rev      abi_revision;     // optional; 0 when the artifact predates it
    fn_owned_str0   registered_families;  // optional; introspection trio
    fn_owned_str0   function_flags;       // optional; introspection trio
    fn_owned_str1   reference_type;       // optional; introspection trio
    fn_filter_compile filter_compile;     // optional; the revision-3 filter trio
    fn_filter_free    filter_free;
    fn_filter_rows    filter_rows;
    fn_block_parse    block_parse;         // optional; the revision-4 block twin
    fn_block_free     block_free;
    fn_filter_eval    filter_eval;
} chs_lib;

static const char * chs_lib_open(const char *path, chs_lib *out) {
    // RTLD_LOCAL is what makes several versions coexist: each library's
    // ClickHouse symbols stay private to it, so two builds that both define
    // DB::DataTypeFactory never collide.
    void *h = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!h) return dlerror();
    out->handle      = h;
    out->version     = (fn_version)     dlsym(h, "chs_clickhouse_version");
    out->init        = (fn_init)        dlsym(h, "chs_init");
    out->freep       = (fn_free)        dlsym(h, "chs_free");
    out->validate    = (fn_validate)    dlsym(h, "chs_validate_type");
    out->compile     = (fn_compile)     dlsym(h, "chs_schema_compile");
    out->schema_free = (fn_schema_free) dlsym(h, "chs_schema_free");
    out->rows        = (fn_rows)        dlsym(h, "chs_rows");
    // Optional: pre-engine artifacts (25.3/25.8 builds before chs_schema_engine)
    // still load; SetEngine on them reports unsupported instead of failing dlopen.
    out->engine      = (fn_engine)      dlsym(h, "chs_schema_engine");
    out->ttl         = (fn_ttl)         dlsym(h, "chs_schema_ttl");
    // Optional: single-row entry and column introspection, so a dlopen'd
    // library can serve the same surface as the statically linked one.
    out->row              = (fn_row)       dlsym(h, "chs_row");
    out->col_count        = (fn_col_count) dlsym(h, "chs_schema_column_count");
    out->col_name         = (fn_col_str)   dlsym(h, "chs_schema_column_name");
    out->col_type         = (fn_col_str)   dlsym(h, "chs_schema_column_type");
    out->col_default_expr = (fn_col_str)   dlsym(h, "chs_schema_column_default_expr");
    out->col_default_kind = (fn_col_str)   dlsym(h, "chs_schema_column_default_kind");
    out->col_default_is_literal = (fn_col_int) dlsym(h, "chs_schema_column_default_is_literal");
    // Optional: an artifact built before the ABI-revision probe reports 0,
    // which docs/reference/artifact.md defines as "unknown", not "incompatible".
    out->abi_revision = (fn_abi_rev) dlsym(h, "chs_abi_revision");
    // Optional: the introspection trio (docs/reference/bindings.md §Introspection).
    // Absence degrades to unsupported at call time, never a load failure.
    out->registered_families = (fn_owned_str0) dlsym(h, "chs_registered_families");
    out->function_flags      = (fn_owned_str0) dlsym(h, "chs_function_flags");
    out->reference_type      = (fn_owned_str1) dlsym(h, "chs_reference_type");
    // Optional: the revision-3 filter trio. Same degradation rule.
    out->filter_compile = (fn_filter_compile) dlsym(h, "chs_filter_compile");
    out->filter_free    = (fn_filter_free)    dlsym(h, "chs_filter_free");
    out->filter_rows    = (fn_filter_rows)    dlsym(h, "chs_filter_rows");
    // Optional: the revision-4 block twin. Same degradation rule.
    out->block_parse = (fn_block_parse) dlsym(h, "chs_block_parse");
    out->block_free  = (fn_block_free)  dlsym(h, "chs_block_free");
    out->filter_eval = (fn_filter_eval) dlsym(h, "chs_filter_eval");
    // Mandatory = the original core API, exported by every artifact ever
    // shipped: version/init/compile/rows AND free/validate/schema_free. The
    // shims below call the latter three without NULL checks, so admitting a
    // library missing one would trade a clean load error for a SIGSEGV on
    // first use (the optional groups above DO degrade gracefully instead).
    if (!out->version || !out->init || !out->rows || !out->compile
        || !out->freep || !out->validate || !out->schema_free) {
        dlclose(h);
        return "library does not export the chtypes C API";
    }
    return NULL;
}

static const char * chs_lib_version(chs_lib *l)                  { return l->version(); }
// 0 when the artifact predates the probe: absence is ignorance, not a claim.
static int          chs_lib_abi_revision(chs_lib *l)              { return l->abi_revision ? l->abi_revision() : 0; }
static int          chs_lib_init(chs_lib *l, const char *tz, const char *u, char **err) { return l->init(tz, u, err); }
static void         chs_lib_free(chs_lib *l, char *p)            { l->freep(p); }
// chs_schema_compile is mandatory (see chs_lib_open below), so presence of
// the consolidated, settings-aware symbol is true of every artifact that
// loads at all here — but the probe is still a real dlsym check, not a
// constant, because a Registry may someday load a third-party-built
// artifact that only exports the pre-consolidation shape under this name.
static int          chs_lib_has_compile(chs_lib *l)              { return l->compile != NULL; }
static void *       chs_lib_compile(chs_lib *l, const char *s, const char *j, int mode, int *c, char **e) {
    return l->compile(s, j, mode, c, e);
}
static void         chs_lib_schema_free(chs_lib *l, void *s)     { l->schema_free(s); }
static char *       chs_lib_rows(chs_lib *l, const void *s, int f, const char *b, size_t n, const char *st) {
    // -1 = CHS_EXPORT_NONE, 7u = CHS_DOC_ALL, no export buffer — the
    // revision-3 pass-through that keeps Rows() byte-identical to revision 2.
    return l->rows(s, f, b, n, st, -1, 7u, (chs_lib_bytes *)0);
}
// The export spelling: RowsExport's one call. ob may be NULL only when
// ef == -1 (the library initializes *ob to {NULL,0} at entry otherwise).
static char * chs_lib_rows_export(chs_lib *l, const void *s, int f, const char *b, size_t n,
                                  const char *st, int ef, unsigned df, chs_lib_bytes *ob) {
    return l->rows(s, f, b, n, st, ef, df, ob);
}
// The filter trio. has_filter is all-or-nothing like the introspection
// checks: the three symbols shipped together at revision 3.
static int chs_lib_has_filter(chs_lib *l) {
    return l->filter_compile && l->filter_free && l->filter_rows;
}
// p = params_json (revision 4): the {name:Type} bindings, a JSON object of
// name -> value STRING; "{}"/NULL declares none.
static void * chs_lib_filter_compile(chs_lib *l, const void *s, const char *e, const char *p, int *c, char **err) {
    return l->filter_compile(s, e, p, c, err);
}
static void chs_lib_filter_free(chs_lib *l, void *f) { l->filter_free(f); }
static char * chs_lib_filter_rows(chs_lib *l, const void *f, int fmt, const char *b, size_t n, const char *st) {
    return l->filter_rows(f, fmt, b, n, st);
}
// The revision-4 block twin. has_block is all-or-nothing like the filter
// check: the three symbols shipped together at revision 4 (with chs_filter_eval
// counted here — it needs a block to mean anything).
static int chs_lib_has_block(chs_lib *l) {
    return l->block_parse && l->block_free && l->filter_eval;
}
static void * chs_lib_block_parse(chs_lib *l, const void *s, int fmt, const char *b, size_t n,
                                  const char *st, int *c, char **err) {
    return l->block_parse(s, fmt, b, n, st, c, err);
}
static void chs_lib_block_free(chs_lib *l, void *b) { l->block_free(b); }
static char * chs_lib_filter_eval(chs_lib *l, const void *f, const void *b) {
    return l->filter_eval(f, b);
}
static int chs_lib_engine(chs_lib *l, void *s, const char *e, const char *o, const char *mt, char **err) {
    if (!l->engine) return -3; // artifact predates chs_schema_engine
    return l->engine(s, e, o, mt, err);
}
static int chs_lib_ttl(chs_lib *l, void *s, const char *t, char **err) {
    if (!l->ttl) return -3; // artifact predates chs_schema_ttl
    return l->ttl(s, t, err);
}
static char * chs_lib_row(chs_lib *l, const void *s, int f, const char *b, size_t n, const char *st) {
    if (!l->row) return NULL; // artifact predates chs_row
    return l->row(s, f, b, n, st);
}
static int chs_lib_validate(chs_lib *l, const char *e, char **canon, int *code, char **err) {
    return l->validate(e, canon, code, err);
}
// Column introspection is all-or-nothing: an artifact either exports the whole
// group (they shipped together) or none of it. col_count answering -1 is the
// "none" signal the Go side keys on.
static int chs_lib_col_count(chs_lib *l, const void *s) {
    if (!l->col_count || !l->col_name || !l->col_type
        || !l->col_default_expr || !l->col_default_kind || !l->col_default_is_literal)
        return -1;
    return l->col_count(s);
}
// The introspection trio. NULL means "the artifact predates the symbol" —
// the Go side degrades it to an *UnsupportedError, never a crash.
static char * chs_lib_registered_families(chs_lib *l) {
    if (!l->registered_families) return NULL;
    return l->registered_families();
}
static char * chs_lib_function_flags(chs_lib *l) {
    if (!l->function_flags) return NULL;
    return l->function_flags();
}
static char * chs_lib_reference_type(chs_lib *l, const char *e) {
    if (!l->reference_type) return NULL;
    return l->reference_type(e);
}
static const char * chs_lib_col_name(chs_lib *l, const void *s, int i)         { return l->col_name(s, i); }
static const char * chs_lib_col_type(chs_lib *l, const void *s, int i)         { return l->col_type(s, i); }
static const char * chs_lib_col_default_expr(chs_lib *l, const void *s, int i) { return l->col_default_expr(s, i); }
static const char * chs_lib_col_default_kind(chs_lib *l, const void *s, int i) { return l->col_default_kind(s, i); }
static int          chs_lib_col_is_literal(chs_lib *l, const void *s, int i)   { return l->col_default_is_literal(s, i); }
*/
import "C"

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"unsafe"
)

// ---------------------------------------------------------------- locking
//
// The C ABI states its contract in include/chtypes.h:
//
//	"The library is thread-safe for concurrent chs_row() calls on distinct
//	 handles; a single handle must not be used from two threads at once."
//
// This package enforces exactly that, at two levels. It used to hold ONE mutex
// per Library across every call, which serialized unrelated schemas against
// each other and was strictly stronger than the ABI promises.
//
// HANDLE LEVEL — LoadedSchema.mu, exclusive. One handle, one thread at a time,
// which is the header's second clause verbatim:
//
//	chs_row, chs_rows          `const chs_schema *`. The header names chs_row;
//	                           chs_rows is its batch sibling with the identical
//	                           const-handle signature, documented immediately
//	                           below it, and covered by the same STATELESSNESS
//	                           block ("Every entry point below is a pure
//	                           function of (build version, schema text, row
//	                           bytes, settings, clock instant)").
//	chs_schema_engine,         `chs_schema *` — NON-const. These MUTATE the
//	chs_schema_ttl             handle, so they need the same exclusion as a
//	                           writer; "must not be used from two threads at
//	                           once" covers them, and the non-const signature
//	                           is why they cannot be demoted to a read lock.
//	chs_schema_column_*        const reads, but they return BORROWED pointers
//	                           "valid until chs_schema_free()", so they must not
//	                           race the free.
//	chs_schema_free            destroys the handle.
//	chs_free                   plain free() of a result string; kept on the
//	                           handle lock because it is always paired with the
//	                           call that produced the pointer.
//
// LIBRARY LEVEL — Library.mu, a RWMutex. RTLD_LOCAL is what makes this the
// right scope: each artifact keeps its own copy of ClickHouse's symbols AND of
// the wrapper's process-globals, so "process-wide" state in chtypes.cpp is in
// fact per-Library here.
//
//	READ   chs_schema_compile, chs_validate_type, and every handle-level call
//	       above. They only READ that per-library state, so they proceed
//	       concurrently with each other.
//	WRITE  nothing, by construction — and that is the point. chs_init is the one
//	       writer (it rebuilds the refuse-list and re-sets DateLUT's default
//	       timezone on every call, while the row and compile paths read the
//	       refuse-list BY REFERENCE), so instead of locking every call against
//	       it, openLibrary runs it exactly ONCE PER ARTIFACT PATH per process,
//	       before the Library is reachable by any other goroutine. The write
//	       side therefore never has to be taken at all. The header does not
//	       discuss chs_init's concurrency; the wrapper source is the reason this
//	       is handled rather than assumed.
//
// The RWMutex is kept even though nothing currently takes it exclusively: it is
// what a future writer (a re-init, a settings seed on this path) must acquire,
// and Library.mu is the documented place to do it.
//
// DELIBERATELY NOT GUARDED, because this path cannot reach them: the function
// pointer table (chs_lib) does not dlsym chs_set_default_settings or
// chs_shutdown. chs_set_default_settings is the one genuinely dangerous
// call in the ABI — it reallocates a process-global the row path reads by
// reference — and a Library structurally cannot make it. On the statically
// linked path it IS reachable, and chtypes.go guards it (see defaultSettingsMu).
// (The read-only introspection trio — chs_registered_families,
// chs_function_flags, chs_reference_type — joined the table 2026-08-26 for
// SDK parity, docs/reference/bindings.md §Introspection. All three are READERS and take
// the shared side like every other call.)
//
// WHAT IS PROVEN, AND HOW FAR. Both chs_row and chs_rows reach ClickHouse's
// evaluateMissingDefaults / TTLDescription::buildExpression with ONE shared
// global Context per library, rather than the per-query context copy upstream
// ClickHouse makes. The header asserts chs_row is safe on distinct handles and
// that is the contract this package implements. A source audit could not
// upgrade that assertion to a proof, and Go's -race detector does not see
// inside C — so tests/tsan/ holds the instrument that can: a SANITIZE=thread
// build of the vendored tree plus the unmodified wrapper, statically linked
// into a TSan-compiled stress driver that mirrors these tests' workload
// (steady distinct handles + compile/engine churn + validate, DEFAULT/TTL
// evaluation included). Its runs are TSan-clean with ZERO suppressions
// (tests/tsan/RESULTS.md — the run of record: ~33 M concurrent calls, 0
// reports, 0 mismatches). SCOPE, stated rather than implied: one version
// (25.8, the reference line), one platform (darwin-arm64), that workload,
// and the executed interleavings — other vendored lines share this wrapper
// source but are separate builds, and the same-handle-two-threads case
// remains documented UB, not a proven tolerance. The stress tests below
// therefore still assert the thing that is observable from Go on every
// platform and every version: concurrent answers equal single-threaded
// answers, case for case.

// Library is one dlopen'd vendored ClickHouse build, obtained from
// Registry.For (or Registry.Load). It names itself by calling
// chs_clickhouse_version — nothing is inferred from the file path. Each
// loaded version costs roughly 120 MB resident. Safe for concurrent use;
// there is deliberately no close/shutdown — a Library is never dlclose'd, so
// none is owed (docs/reference/bindings.md §Teardown), and it structurally cannot call
// chs_set_default_settings.
type Library struct {
	Version Version // the exact patch, e.g. "25.8.28.1-lts"
	Minor   string  // the line the arbiter and callers ask in, e.g. "25.8"
	Path    string
	// ABIRevision is the chs_* ABI revision THIS ARTIFACT was built from,
	// or 0 when it predates chs_abi_revision. A nonzero value that differs
	// from the package's own ABIRevision is refused at load time, so a
	// Library that exists has either an equal revision or an unknown one.
	ABIRevision int

	lib C.chs_lib
	// mu is a RWMutex over this library's own process-globals: exclusive for
	// chs_init, shared for every call that reads them. See "locking" above.
	mu sync.RWMutex
}

// initMu guards dlopen + chs_init and the loadedLibs table below. Held only at
// load time, never on a call path, so it costs a startup mutex and nothing else.
var (
	initMu     sync.Mutex
	loadedLibs = map[string]*Library{}
)

// Registry holds one Library per ClickHouse version and dispatches by
// version — the multi-version product path. Safe for concurrent use.
//
// Lookup follows the docs/guides/fetch.md §1 search path: the directory given to
// NewRegistry (loaded eagerly, as it always was), then $CHTYPES_REGISTRY,
// the per-user cache and the system locations, each consulted lazily by
// For for a line the loaded set lacks. With AutoFetch (WithAutoFetch, or
// CHTYPES_AUTOFETCH=1) a line found nowhere is fetched first (Ensure),
// once per process per line; without it, the miss is ErrArtifactMissing.
type Registry struct {
	mu   sync.RWMutex
	byID map[string]*Library
	// known maps a minor line to the artifact directory discovered for it
	// on the search path at construction (a lazy registry), whether or not
	// it has been dlopen'd yet. Guarded by mu.
	known map[string]string

	explicit  string   // the constructor's directory, "" for the search path alone
	search    []string // the §1 search path, in order
	autoFetch bool
	fetch     FetchOptions
}

// RegistryOption configures NewRegistry.
type RegistryOption func(*Registry)

// WithAutoFetch turns lazy fetch on first open on or off for this registry
// (CHTYPES_AUTOFETCH=1 turns it on for every registry). Off by default: a
// production process must not begin a 250 MB download inside a request.
func WithAutoFetch(on bool) RegistryOption { return func(r *Registry) { r.autoFetch = on } }

// WithFetchOptions sets the options a lazy fetch runs with — the source,
// the trust list, a lock file, progress output. Dest defaults to the
// registry's own write directory (§1) and Platform is always this host's:
// a registry only ever dlopens artifacts it can run.
func WithFetchOptions(o FetchOptions) RegistryOption { return func(r *Registry) { r.fetch = o } }

// NewRegistry opens a registry. With a directory, every artifact under it
// is loaded now — one directory per version, each holding the manifest.json
// the build writes:
//
//	dir/25.8/{manifest.json,libchtypes.so}
//	dir/26.6/{manifest.json,libchtypes.so}
//
// — and it is an error for that directory to hold nothing (unless
// AutoFetch is on, in which case a fetch will populate it). With "" the
// registry is the §1 search path alone: nothing is dlopen'd until For asks
// for a line, and it is an error for the whole path to hold nothing
// (again unless AutoFetch is on).
func NewRegistry(dir string, opts ...RegistryOption) (*Registry, error) {
	r := &Registry{byID: map[string]*Library{}, known: map[string]string{}, explicit: dir}
	for _, o := range opts {
		o(r)
	}
	if os.Getenv(envAutoFetch) == "1" {
		r.autoFetch = true
	}
	r.search = RegistrySearchPath(dir)
	if dir == "" {
		r.discover()
		if len(r.known) == 0 && !r.autoFetch {
			return nil, fmt.Errorf("chtypes: no version artifacts on the registry search path (looked in: %s)", strings.Join(r.search, ", "))
		}
		return r, nil
	}
	loaded, err := r.loadDir(dir)
	if err != nil {
		if !(r.autoFetch && os.IsNotExist(err)) {
			return nil, err
		}
	}
	if loaded == 0 && !r.autoFetch {
		return nil, fmt.Errorf("chtypes: no version artifacts under %s", dir)
	}
	return r, nil
}

// loadDir dlopens every artifact directory under dir and returns how many
// it loaded. A directory without a readable manifest.json is skipped; one
// whose library fails to load is an error naming it.
func (r *Registry) loadDir(dir string) (int, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, err
	}
	var loaded int
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		sub := filepath.Join(dir, e.Name())
		m, ok := readArtifactDir(sub)
		if !ok {
			continue
		}
		if err := r.Load(filepath.Join(sub, m.Library)); err != nil {
			return loaded, fmt.Errorf("%s: %w", sub, err)
		}
		loaded++
	}
	return loaded, nil
}

// readArtifactDir reads one <minor>/manifest.json; ok is false when the
// directory is not an artifact directory.
func readArtifactDir(sub string) (m struct {
	Library string `json:"library"`
	Version string `json:"clickhouse_version"`
	Minor   string `json:"clickhouse_minor"`
}, ok bool) {
	mf, err := os.ReadFile(filepath.Join(sub, "manifest.json"))
	if err != nil {
		return m, false
	}
	if err := json.Unmarshal(mf, &m); err != nil || m.Library == "" {
		return m, false
	}
	return m, true
}

// Load dlopens one library and registers it under its own reported version.
// Loading the same path twice — into this Registry or another one — reuses the
// Library that was already initialized for it.
func (r *Registry) Load(path string) error {
	lib, err := openLibrary(path)
	if err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.byID[string(lib.Version)] = lib
	r.byID[lib.Minor] = lib
	return nil
}

// openLibrary dlopens and chs_inits one artifact AT MOST ONCE PER PROCESS,
// keyed on its path, and hands back the same *Library to every later caller.
//
// The deduplication is a correctness requirement, not a cache. dlopen is
// refcounted per path: opening one artifact twice yields ONE address space and
// therefore ONE set of the wrapper's globals. chs_init is not idempotent
// bookkeeping over those — it rebuilds the refuse-list (clear, then repopulate)
// and re-sets DateLUT's default timezone, unconditionally, and the row and
// compile paths read the refuse-list BY REFERENCE. So a second chs_init for a
// path already in use would reallocate a vector out from under live readers.
// Initializing once removes that hazard at the source, which is better than
// putting a process-wide lock on every call to tolerate it.
//
// This is also what makes Library.mu's write side trivially safe: chs_init runs
// while the Library is still unreachable by any other goroutine, so nothing can
// hold the read lock during it.
func openLibrary(path string) (*Library, error) {
	initMu.Lock()
	defer initMu.Unlock()

	// Key on the RESOLVED path, not the spelling: dlopen refcounts one image
	// per file, so `./x/libchtypes.so` and its absolute spelling — or a
	// legacy-name symlink like the libchtypes.so -> libchtypes_s1.so bridge
	// ci/steps/stage-lib-build.sh stages — are the SAME image, and missing
	// the map here would run chs_init a second time on live state (the "AT
	// MOST ONCE PER PROCESS" invariant below).
	key := path
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		if abs, err := filepath.Abs(resolved); err == nil {
			key = abs
		}
	}
	if lib, ok := loadedLibs[key]; ok {
		return lib, nil
	}

	lib := &Library{Path: path}
	cpath := C.CString(path)
	defer C.free(unsafe.Pointer(cpath))

	if e := C.chs_lib_open(cpath, &lib.lib); e != nil {
		return nil, fmt.Errorf("dlopen %s: %s", path, C.GoString(e))
	}
	// The library names itself; nothing is inferred from the path.
	lib.Version = Version(C.GoString(C.chs_lib_version(&lib.lib)))
	lib.Minor = minorOf(string(lib.Version))

	// The ABI identity gate (docs/reference/c-abi.md §ABI identity). A DIFFERENT nonzero
	// revision is a positive statement that these declarations do not describe
	// this artifact, so calling through them would be undefined — refuse, and
	// say both numbers. Revision 0 means the artifact predates the probe and
	// keeps the pre-existing per-symbol degradation rules.
	lib.ABIRevision = int(C.chs_lib_abi_revision(&lib.lib))
	//
	// Not dlclose'd, exactly like the chs_init failure below: this package
	// never unmaps a library whose ClickHouse globals may already exist (see
	// the module docs on ManuallyDrop/never-dropped). The mapping is leaked
	// and the Library is not returned.
	if lib.ABIRevision != 0 && lib.ABIRevision != ABIRevision {
		return nil, fmt.Errorf(
			"chtypes: %s reports ABI revision %d, this package speaks %d; "+
				"refusing to call through mismatched declarations", path, lib.ABIRevision, ABIRevision)
	}

	// Each library keeps its own DateLUT and its own refuse-list.
	tz := C.CString(Timezone)
	defer C.free(unsafe.Pointer(tz))
	ul := ""
	if b, err := os.ReadFile(filepath.Join(filepath.Dir(path), "unsafe_families.txt")); err == nil {
		ul = strings.TrimSpace(string(b))
	}
	cul := C.CString(ul)
	defer C.free(unsafe.Pointer(cul))
	var cErr *C.char
	if rc := C.chs_lib_init(&lib.lib, tz, cul, &cErr); rc != 0 {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_lib_free(&lib.lib, cErr)
		}
		return nil, fmt.Errorf("chs_init failed for %s: %d: %s", path, int(rc), msg)
	}

	loadedLibs[key] = lib
	return lib, nil
}

// Versions lists the ClickHouse minor lines this registry can answer for:
// every loaded library's line, plus — for a registry opened on the search
// path alone — every line discovered there at construction.
func (r *Registry) Versions() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.versionsLocked()
}

// Libraries lists the libraries this registry has actually LOADED, one per
// line, in numeric release order — the same order Versions uses, because
// docs/reference/bindings.md §Version selection rule 2 governs "every ordered surface a
// binding exposes" and two of them sorting differently is the bug that rule was
// written after.
//
// It never loads anything: a lazily-discovered line that no For call has opened
// yet appears in Versions and not here, which is what the peer bindings' own
// libraries() answers (TypeScript and Rust both list "what has been opened so
// far"). Opening 120 MB of artifact as the side effect of a listing call is not
// something a caller can undo.
func (r *Registry) Libraries() []*Library {
	r.mu.RLock()
	defer r.mu.RUnlock()
	seen := map[*Library]bool{}
	out := make([]*Library, 0, len(r.byID))
	for _, l := range r.byID {
		if !seen[l] {
			seen[l] = true
			out = append(out, l)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		return minorSortKey(out[i].Minor).before(minorSortKey(out[j].Minor))
	})
	return out
}

// For resolves a version to its library. A minor line ("25.8") or an exact
// patch ("25.8.28.1-lts") both work: lookup tries the given string first,
// then its minor line, so a drifted docker patch tag still finds its line.
// Resolution never falls back to the nearest version, because answering
// 26.7 semantics from a 25.8 artifact would be silently wrong.
//
// A line the loaded set lacks is looked for along the §1 search path and
// loaded from the first directory that holds it; a line found nowhere is
// fetched first when AutoFetch is on, and is otherwise ErrArtifactMissing
// (errors.Is), with the §7 message naming every directory looked in.
// ForContext is the same with a context for the fetch.
func (r *Registry) For(v Version) (*Library, error) {
	return r.ForContext(context.Background(), v)
}

// lookup answers from what is loaded: the exact spelling first, then the
// minor line.
func (r *Registry) lookup(v Version) *Library {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if l, ok := r.byID[string(v)]; ok {
		return l
	}
	if l, ok := r.byID[minorOf(string(v))]; ok {
		return l
	}
	return nil
}

func (r *Registry) versionsLocked() []string {
	seen := map[string]bool{}
	var out []string
	for _, l := range r.byID {
		if !seen[l.Minor] {
			seen[l.Minor] = true
			out = append(out, l.Minor)
		}
	}
	for minor := range r.known {
		if !seen[minor] {
			seen[minor] = true
			out = append(out, minor)
		}
	}
	sortMinorLines(out)
	return out
}

// LoadedSchema is a schema compiled inside one specific version's library —
// the Registry-path twin of CompiledSchema, with the same contract: one
// handle is single-threaded (enforced internally), distinct handles proceed
// in parallel, Close releases the native handle.
type LoadedSchema struct {
	// Columns/Canonical/LiteralDefault mirror CompiledSchema's fields. They are
	// populated when the artifact exports the column-introspection API and left
	// empty (with Columns nil) when it predates it.
	Columns        []Column
	Canonical      []string
	LiteralDefault []bool

	lib    *Library
	handle unsafe.Pointer
	// mu makes this ONE handle single-threaded, which is the whole of the
	// header's second clause. Distinct LoadedSchemas never contend.
	mu sync.Mutex
	// filters tracks every open LoadedFilter compiled from this handle, so
	// Close can free them FIRST (docs/reference/c-abi.md §Filters, handle lifetime).
	// Guarded by mu.
	filters map[*LoadedFilter]struct{}
	// blocks tracks every open LoadedBlock parsed from this handle — the
	// same non-owning rule, the same free-before-schema order (docs/reference/c-abi.md
	// §Blocks). Guarded by mu.
	blocks map[*LoadedBlock]struct{}
}

// lock takes both levels in the one legal order: the library's read lock (so no
// chs_init can be in flight) and then this handle's exclusive lock. Always
// released by the returned func, so every call site is one deferred line and
// none of them can invent a different order and deadlock.
func (s *LoadedSchema) lock() func() {
	s.lib.mu.RLock()
	s.mu.Lock()
	return func() {
		s.mu.Unlock()
		s.lib.mu.RUnlock()
	}
}

// CompileDDL compiles a column list against this library's ClickHouse,
// optionally under a DECLARED settings profile (WithCompileSettings) and
// compile mode (WithCompileMode) — see the package-level CompileDDL for the
// full contract; this is its Registry-path twin. With no options this is the
// same compile-base path the call has always taken.
func (l *Library) CompileDDL(ddl string, opts ...CompileOption) (*LoadedSchema, error) {
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

	// Read lock, not exclusive: compile READS this library's globals (the
	// refuse-list, the seeded settings, the global Context) and returns a fresh
	// handle nothing else can see yet. Only chs_init writes them, and it holds
	// the write lock. Concurrent compiles on one Library are therefore allowed;
	// tighten this to Lock/Unlock if that ever proves optimistic.
	l.mu.RLock()
	h := C.chs_lib_compile(&l.lib, cddl, csj, C.int(cfg.mode), &code, &cErr)
	if h == nil {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_lib_free(&l.lib, cErr)
		}
		l.mu.RUnlock()
		return nil, schemaErr(int(code), msg, "")
	}
	s := l.newLoadedSchema(unsafe.Pointer(h))
	l.mu.RUnlock()
	return s, nil
}

// newLoadedSchema wraps a fresh C handle and mirrors the column-introspection
// group into Go. The caller holds l.mu's read side; the handle is not yet
// visible to any other goroutine.
func (l *Library) newLoadedSchema(h unsafe.Pointer) *LoadedSchema {
	s := &LoadedSchema{lib: l, handle: h}
	if n := int(C.chs_lib_col_count(&l.lib, s.handle)); n >= 0 {
		for i := 0; i < n; i++ {
			ci := C.int(i)
			s.Columns = append(s.Columns, Column{
				Name:        C.GoString(C.chs_lib_col_name(&l.lib, s.handle, ci)),
				Type:        C.GoString(C.chs_lib_col_type(&l.lib, s.handle, ci)),
				Default:     C.GoString(C.chs_lib_col_default_expr(&l.lib, s.handle, ci)),
				DefaultKind: parseDefaultKind(C.GoString(C.chs_lib_col_default_kind(&l.lib, s.handle, ci))),
			})
			s.Canonical = append(s.Canonical, s.Columns[i].Type)
			s.LiteralDefault = append(s.LiteralDefault, C.chs_lib_col_is_literal(&l.lib, s.handle, ci) != 0)
		}
	}
	return s
}

// HasCompileSettings reports whether this artifact exports the consolidated,
// settings-aware chs_schema_compile symbol. True for every artifact this
// repo builds — chs_schema_compile is mandatory for a Library to load at
// all (see chs_lib_open) — but it is a real dlsym-backed check rather than a
// constant, because a Registry may someday load a third-party-built
// artifact. The conformance driver's `caps.compile_settings` handshake keys
// on it.
func (l *Library) HasCompileSettings() bool {
	return C.chs_lib_has_compile(&l.lib) != 0
}

// ValidateType mirrors the package-level ValidateType for a dlopen'd
// library: the same canonicalization and the same *SchemaError /
// *UnsupportedError split, answered by THIS library's ClickHouse version.
func (l *Library) ValidateType(typeExpr string) (canonical string, err error) {
	cexpr := C.CString(typeExpr)
	defer C.free(unsafe.Pointer(cexpr))
	var cCanon, cErr *C.char
	var code C.int
	// Read lock: chs_validate_type reads only the refuse-list and the
	// (immutable-after-init) DataTypeFactory registry. No handle is involved.
	l.mu.RLock()
	rc := C.chs_lib_validate(&l.lib, cexpr, &cCanon, &code, &cErr)
	if rc != 0 {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_lib_free(&l.lib, cErr)
		}
		l.mu.RUnlock()
		return "", schemaErr(int(code), msg, "")
	}
	canonical = C.GoString(cCanon)
	C.chs_lib_free(&l.lib, cCanon)
	l.mu.RUnlock()
	return canonical, nil
}

// RegisteredFamilies lists every type family in THIS library's runtime
// registry (chs_registered_families) — the dlopen'd twin of the package-level
// RegisteredFamilies, and one of the three-question introspection surface
// every SDK exposes (docs/reference/bindings.md §Introspection). An artifact built
// before the symbol answers an *UnsupportedError, never a load failure.
func (l *Library) RegisteredFamilies() ([]string, error) {
	l.mu.RLock()
	c := C.chs_lib_registered_families(&l.lib)
	if c == nil {
		l.mu.RUnlock()
		return nil, &UnsupportedError{Msg: "this artifact predates chs_registered_families (rebuild it)"}
	}
	s := C.GoString(c)
	C.chs_lib_free(&l.lib, c)
	l.mu.RUnlock()
	var out []string
	for _, line := range strings.Split(s, "\n") {
		if line != "" {
			out = append(out, line)
		}
	}
	return out, nil
}

// FunctionFlags returns THIS library's function-volatility TSV audit
// (chs_function_flags), verbatim — one function per line, six tab-separated
// fields; see the package-level FunctionFlags for the field list. One of the
// three-question introspection surface every SDK exposes (docs/reference/bindings.md
// §Introspection). An artifact built before the symbol answers an
// *UnsupportedError.
func (l *Library) FunctionFlags() (string, error) {
	l.mu.RLock()
	c := C.chs_lib_function_flags(&l.lib)
	if c == nil {
		l.mu.RUnlock()
		return "", &UnsupportedError{Msg: "this artifact predates chs_function_flags (rebuild it)"}
	}
	s := C.GoString(c)
	C.chs_lib_free(&l.lib, c)
	l.mu.RUnlock()
	return s, nil
}

// ReferenceType returns the widened reference type THIS library compares a
// type against (chs_reference_type), "" for a type with no wider type — the
// dlopen'd twin of the package-level ReferenceType, and one of the
// three-question introspection surface every SDK exposes (docs/reference/bindings.md
// §Introspection). An artifact built before the symbol answers an
// *UnsupportedError.
func (l *Library) ReferenceType(typeExpr string) (string, error) {
	cexpr := C.CString(typeExpr)
	defer C.free(unsafe.Pointer(cexpr))
	l.mu.RLock()
	c := C.chs_lib_reference_type(&l.lib, cexpr)
	if c == nil {
		l.mu.RUnlock()
		return "", &UnsupportedError{Msg: "this artifact predates chs_reference_type (rebuild it)"}
	}
	s := C.GoString(c)
	C.chs_lib_free(&l.lib, c)
	l.mu.RUnlock()
	return s, nil
}

// SetEngine mirrors CompiledSchema.SetEngine for a dlopen'd library,
// including WithMergeTreeSettings. An artifact built before chs_schema_engine
// existed reports unsupported rather than failing to load.
//
// Names are validated by the server's own MergeTreeSettings object: an
// unknown name answers the server's code 115. A known name declared at a
// NON-default value is refused (an *UnsupportedError naming it) — no MergeTree
// setting's behavior is modeled yet, and silently ignoring a declared value
// would mean the declared profile is not in force. Declared at the default is
// inert and accepted.
func (s *LoadedSchema) SetEngine(engine, orderBy string, opts ...EngineOption) error {
	var cfg engineConfig
	for _, opt := range opts {
		opt(&cfg)
	}
	ce := C.CString(engine)
	defer C.free(unsafe.Pointer(ce))
	co := C.CString(orderBy)
	defer C.free(unsafe.Pointer(co))
	cmt := C.CString(settingsJSON(cfg.mergeTreeSettings))
	defer C.free(unsafe.Pointer(cmt))
	var cErr *C.char
	// chs_schema_engine takes a NON-const handle: it is a writer. The nil check
	// moves inside the lock so a concurrent Close cannot free between the two.
	unlock := s.lock()
	if s.handle == nil {
		unlock()
		return fmt.Errorf("chtypes: schema is closed")
	}
	rc := C.chs_lib_engine(&s.lib.lib, s.handle, ce, co, cmt, &cErr)
	msg := ""
	if cErr != nil {
		msg = C.GoString(cErr)
		C.chs_lib_free(&s.lib.lib, cErr)
	}
	unlock()
	if rc == 0 {
		return nil
	}
	if rc == -3 {
		msg = "this artifact predates engine support (rebuild it)"
	}
	// Same rule as the static path: a positive rc is the SERVER's refusal,
	// passed through with its own code and message as a *SchemaError; a
	// negative rc (incl. the shim's own -3 for a missing symbol) is this
	// library declining, and becomes an *UnsupportedError. schemaErr keys on
	// the sign, so -3 needs no special case here.
	return schemaErr(int(rc), msg, "")
}

// SetTTL mirrors CompiledSchema.SetTTL for a dlopen'd library: every
// refusal is an *UnsupportedError, including an artifact built before
// chs_schema_ttl existed.
func (s *LoadedSchema) SetTTL(ttl string) error {
	ct := C.CString(ttl)
	defer C.free(unsafe.Pointer(ct))
	var cErr *C.char
	// Also a NON-const handle, so also a writer. See SetEngine.
	unlock := s.lock()
	if s.handle == nil {
		unlock()
		return fmt.Errorf("chtypes: schema is closed")
	}
	rc := C.chs_lib_ttl(&s.lib.lib, s.handle, ct, &cErr)
	msg := ""
	if cErr != nil {
		msg = C.GoString(cErr)
		C.chs_lib_free(&s.lib.lib, cErr)
	}
	unlock()
	if rc == 0 {
		return nil
	}
	if rc == -3 {
		msg = "this artifact predates TTL support (rebuild it)"
	}
	return &UnsupportedError{Msg: msg}
}

// Close releases the native schema. Idempotent, and safe to race with calls on
// the same handle: whoever gets the lock first wins and the rest see a closed
// schema rather than a freed one. Any LoadedFilter still open on this schema
// is closed FIRST — the filter-before-schema free order the C layer requires.
func (s *LoadedSchema) Close() {
	unlock := s.lock()
	defer unlock()
	for f := range s.filters {
		f.closeLocked()
	}
	s.filters = nil
	for b := range s.blocks {
		b.closeLocked()
	}
	s.blocks = nil
	if s.handle == nil {
		return
	}
	C.chs_lib_schema_free(&s.lib.lib, s.handle)
	s.handle = nil
}

// Row validates and coerces a single row body, mirroring CompiledSchema.Row.
func (s *LoadedSchema) Row(format Format, raw []byte) (RowResult, error) {
	return s.RowWithSettings(format, raw, nil)
}

// RowWithSettings mirrors CompiledSchema.RowWithSettings for a dlopen'd
// library. An artifact built before chs_row reports an error rather than
// guessing at batch semantics.
func (s *LoadedSchema) RowWithSettings(format Format, raw []byte, settings map[string]string) (RowResult, error) {
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

	// One critical section for the call, the result read and the free: the
	// header's "a single handle must not be used from two threads at once".
	// Distinct handles run this concurrently — that is the whole relaxation.
	unlock := s.lock()
	if s.handle == nil {
		unlock()
		return RowResult{}, fmt.Errorf("chtypes: schema is closed")
	}
	out := C.chs_lib_row(&s.lib.lib, s.handle, C.int(format), praw, C.size_t(len(raw)), csj)
	runtime.KeepAlive(raw)
	if out == nil {
		unlock()
		// A missing symbol is the DECLINE type, not a plain error: "this
		// artifact predates the feature" degrades to unsupported at call time
		// (docs/reference/bindings.md Level 1; rule 12's missing-symbol arm). A plain
		// error here read as a caller fault and could not be handled as the
		// decline it is.
		return RowResult{}, &UnsupportedError{Msg: "this artifact predates chs_row (rebuild it)"}
	}
	js := C.GoString(out)
	C.chs_lib_free(&s.lib.lib, out)
	unlock()

	var doc rowDoc
	if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
		return RowResult{}, fmt.Errorf("chtypes: bad result document: %w", err)
	}
	return rowResultOf(doc), nil
}

// Rows coerces a request body through this library's ClickHouse, mirroring
// CompiledSchema.Rows: same parameters, same settings precedence, same
// BatchResult contract (the error return is only for a closed schema or a
// missing symbol, never a ClickHouse verdict).
func (s *LoadedSchema) Rows(format Format, body []byte, settings map[string]string) (BatchResult, error) {
	// export off, all document groups on — the same revision-3 pass-through
	// as the static path, so Rows() is byte-identical to revision 2.
	return s.rowsThrough(format, body, settings, ExportNone, DocAll)
}

// RowsExport mirrors CompiledSchema.RowsExport for a dlopen'd library — the
// same signature, the same BatchResult Payload/Spans/ExportDeclined contract,
// one C call. See the static twin for the full doc; the export buffer is
// copied and freed with THIS library's chs_free before returning.
func (s *LoadedSchema) RowsExport(format Format, body []byte, settings map[string]string, exportFormat Format, docFlags ...DocFlags) (BatchResult, error) {
	var flags DocFlags
	for _, f := range docFlags {
		flags |= f
	}
	return s.rowsThrough(format, body, settings, exportFormat, flags)
}

// rowsThrough is the ONE chs_rows call site on the dlopen'd path — the
// static path's twin, with the identical parameter and result contract.
func (s *LoadedSchema) rowsThrough(format Format, body []byte, settings map[string]string, exportFormat Format, flags DocFlags) (BatchResult, error) {
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

	var ob *C.chs_lib_bytes
	var obv C.chs_lib_bytes
	if exportFormat != ExportNone {
		ob = &obv
	}

	// Same one critical section as Row, and the same reasoning: chs_rows takes
	// the identical `const chs_schema *` and is documented alongside chs_row
	// under the STATELESSNESS block that covers every entry point.
	unlock := s.lock()
	if s.handle == nil {
		unlock()
		return BatchResult{}, fmt.Errorf("chtypes: schema is closed")
	}
	out := C.chs_lib_rows_export(&s.lib.lib, s.handle, C.int(format), pbody, C.size_t(len(body)), csj,
		C.int(exportFormat), C.uint(flags), ob)
	runtime.KeepAlive(body)
	// Copy-then-free the export buffer inside the critical section (chs_free
	// pairs with the call that produced the pointer, on the handle lock).
	var payload []byte
	if ob != nil && obv.data != nil {
		if obv.len > 0 {
			payload = C.GoBytes(unsafe.Pointer(obv.data), C.int(obv.len))
		} else {
			// Emitted-empty: data non-NULL, len 0 — distinguishable from a
			// decline's nil.
			payload = []byte{}
		}
		C.chs_lib_free(&s.lib.lib, obv.data)
	}
	if out == nil {
		unlock()
		// Same degradation as Row: a NULL return is the ABI's "the loaded
		// artifact does not export the function" (docs/reference/c-abi.md §Rows), and
		// that is a decline. chs_rows is mandatory on this loader, so today
		// the branch is unreachable — the type still has to be the honest one.
		return BatchResult{}, &UnsupportedError{Msg: "this artifact predates chs_rows (rebuild it)"}
	}
	js := C.GoString(out)
	C.chs_lib_free(&s.lib.lib, out)
	unlock()

	res, err := batchResultOf(js)
	if err != nil {
		return res, err
	}
	res.Payload = payload
	return res, nil
}

// LoadedFilter is the dlopen'd twin of Filter: one boolean expression
// compiled inside one specific version's library, with the same lifetime and
// thread rules — the LoadedFilter references its LoadedSchema, the schema's
// Close closes open filters first, and every filter call takes the schema's
// own handle lock (a filter call IS a use of its schema handle). See Filter
// for the full contract, including the enforcement gate: shadow/replay only
// until the WHERE-truth rig gates green.
type LoadedFilter struct {
	// Expr is the expression text as compiled, for logging and cache keys.
	Expr string

	schema *LoadedSchema
	handle unsafe.Pointer
}

// CompileFilter mirrors CompiledSchema.CompileFilter for a dlopen'd library:
// the same options (WithFilterParams binds {name:Type} query parameters —
// values are STRINGS, never hand-escaped; see the linked twin for the whole
// contract) and the same rule-12 error split (*SchemaError for ClickHouse's
// own refusal — including the server's 456 for an unbound parameter and 457
// for an unparseable value —, *UnsupportedError for this library's decline:
// clock reads, and an artifact built before the filter trio existed).
func (s *LoadedSchema) CompileFilter(expr string, opts ...FilterOption) (*LoadedFilter, error) {
	if C.chs_lib_has_filter(&s.lib.lib) == 0 {
		return nil, &UnsupportedError{Msg: "this artifact predates chs_filter_compile (rebuild it)"}
	}
	var cfg filterConfig
	for _, opt := range opts {
		opt(&cfg)
	}
	cexpr := C.CString(expr)
	defer C.free(unsafe.Pointer(cexpr))
	cparams := C.CString(settingsJSON(cfg.params))
	defer C.free(unsafe.Pointer(cparams))
	var code C.int
	var cErr *C.char
	unlock := s.lock()
	if s.handle == nil {
		unlock()
		return nil, fmt.Errorf("chtypes: schema is closed")
	}
	h := C.chs_lib_filter_compile(&s.lib.lib, s.handle, cexpr, cparams, &code, &cErr)
	if h == nil {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_lib_free(&s.lib.lib, cErr)
		}
		unlock()
		return nil, schemaErr(int(code), msg, "")
	}
	f := &LoadedFilter{Expr: expr, schema: s, handle: unsafe.Pointer(h)}
	if s.filters == nil {
		s.filters = map[*LoadedFilter]struct{}{}
	}
	s.filters[f] = struct{}{}
	unlock()
	runtime.SetFinalizer(f, func(x *LoadedFilter) { x.Close() })
	return f, nil
}

// Rows mirrors Filter.Rows for a dlopen'd library: one chs_filter_rows call,
// verdicts in the FilterResult, the error return only for a closed filter or
// an unreadable document.
func (f *LoadedFilter) Rows(format Format, body []byte, settings map[string]string) (FilterResult, error) {
	s := f.schema
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

	// The schema's own two-level lock: a filter call is a use of its schema
	// handle, so two filters over one schema never run concurrently.
	unlock := s.lock()
	if f.handle == nil {
		unlock()
		return FilterResult{}, fmt.Errorf("chtypes: filter is closed")
	}
	out := C.chs_lib_filter_rows(&s.lib.lib, f.handle, C.int(format), pbody, C.size_t(len(body)), csj)
	runtime.KeepAlive(body)
	if out == nil {
		unlock()
		return FilterResult{}, fmt.Errorf("chtypes: chs_filter_rows returned null")
	}
	js := C.GoString(out)
	C.chs_lib_free(&s.lib.lib, out)
	unlock()

	return filterResultOf(js)
}

// Close releases the native filter. Idempotent, and also performed by the
// schema's own Close (filters first, then the schema — the C-required
// order).
func (f *LoadedFilter) Close() {
	s := f.schema
	if s == nil {
		return
	}
	unlock := s.lock()
	defer unlock()
	f.closeLocked()
}

// closeLocked frees the filter handle. Caller holds the schema's lock.
func (f *LoadedFilter) closeLocked() {
	if f.handle != nil {
		C.chs_lib_filter_free(&f.schema.lib.lib, f.handle)
		f.handle = nil
	}
	if f.schema != nil && f.schema.filters != nil {
		delete(f.schema.filters, f)
	}
}

// LoadedBlock is the dlopen'd twin of Block: one body parsed once inside one
// specific version's library, evaluable by many LoadedFilters sequentially
// with no re-parse. Same lifetime and thread rules — the LoadedBlock
// references its LoadedSchema, the schema's Close closes open blocks first,
// and every block call takes the schema's own handle lock. See Block for the
// full contract.
type LoadedBlock struct {
	schema *LoadedSchema
	handle unsafe.Pointer
}

// ParseBlock mirrors CompiledSchema.ParseBlock for a dlopen'd library: parse
// a body ONCE (chs_block_parse — the parse half of Rows, under this call's
// one clock instant), then evaluate K filters against it with
// LoadedFilter.Eval. The error return carries the call-level refusal (an
// unknown setting's 115, a framing or decode fault — no partial block exists
// on any error), or the decline for an artifact that predates the twin.
func (s *LoadedSchema) ParseBlock(format Format, body []byte, settings map[string]string) (*LoadedBlock, error) {
	if C.chs_lib_has_block(&s.lib.lib) == 0 {
		return nil, &UnsupportedError{Msg: "this artifact predates chs_block_parse (rebuild it)"}
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
	unlock := s.lock()
	if s.handle == nil {
		unlock()
		return nil, fmt.Errorf("chtypes: schema is closed")
	}
	h := C.chs_lib_block_parse(&s.lib.lib, s.handle, C.int(format), pbody, C.size_t(len(body)), csj, &code, &cErr)
	runtime.KeepAlive(body)
	if h == nil {
		msg := ""
		if cErr != nil {
			msg = C.GoString(cErr)
			C.chs_lib_free(&s.lib.lib, cErr)
		}
		unlock()
		return nil, schemaErr(int(code), msg, "")
	}
	b := &LoadedBlock{schema: s, handle: unsafe.Pointer(h)}
	if s.blocks == nil {
		s.blocks = map[*LoadedBlock]struct{}{}
	}
	s.blocks[b] = struct{}{}
	unlock()
	runtime.SetFinalizer(b, func(x *LoadedBlock) { x.Close() })
	return b, nil
}

// Eval mirrors Filter.Eval for a dlopen'd library: one chs_filter_eval call
// over an already-parsed LoadedBlock, answering the SAME document Rows
// answers. A (filter, block) pair from two different schema handles of the
// SAME library answers the C layer's rejected document (code 1002); a pair
// from two different LIBRARIES is refused here — no handle ever crosses a
// dlopen'd image boundary.
func (f *LoadedFilter) Eval(b *LoadedBlock) (FilterResult, error) {
	fs, bs := f.schema, b.schema
	if fs.lib != bs.lib {
		return FilterResult{}, fmt.Errorf("chtypes: filter (ClickHouse %s) and block (ClickHouse %s) come from different libraries", fs.lib.Version, bs.lib.Version)
	}
	var unlock func()
	if fs == bs {
		unlock = fs.lock()
	} else {
		// Two schemas, one library: the library read lock once, then both
		// handle locks in a fixed global order so two crossed Evals cannot
		// deadlock. The C layer answers its rejected-1002 document.
		first, second := fs, bs
		if uintptr(unsafe.Pointer(first)) > uintptr(unsafe.Pointer(second)) {
			first, second = second, first
		}
		fs.lib.mu.RLock()
		first.mu.Lock()
		second.mu.Lock()
		unlock = func() {
			second.mu.Unlock()
			first.mu.Unlock()
			fs.lib.mu.RUnlock()
		}
	}
	if f.handle == nil {
		unlock()
		return FilterResult{}, fmt.Errorf("chtypes: filter is closed")
	}
	if b.handle == nil {
		unlock()
		return FilterResult{}, fmt.Errorf("chtypes: block is closed")
	}
	out := C.chs_lib_filter_eval(&fs.lib.lib, f.handle, b.handle)
	if out == nil {
		unlock()
		return FilterResult{}, fmt.Errorf("chtypes: chs_filter_eval returned null")
	}
	js := C.GoString(out)
	C.chs_lib_free(&fs.lib.lib, out)
	unlock()
	return filterResultOf(js)
}

// Close releases the native block. Idempotent, and also performed by the
// schema's own Close (blocks first, then the schema — the C-required order).
func (b *LoadedBlock) Close() {
	s := b.schema
	if s == nil {
		return
	}
	unlock := s.lock()
	defer unlock()
	b.closeLocked()
}

// closeLocked frees the block handle. Caller holds the schema's lock.
func (b *LoadedBlock) closeLocked() {
	if b.handle != nil {
		C.chs_lib_block_free(&b.schema.lib.lib, b.handle)
		b.handle = nil
	}
	if b.schema != nil && b.schema.blocks != nil {
		delete(b.schema.blocks, b)
	}
}

// sortMinorLines orders minor lines numerically. docs/reference/bindings.md §Version
// selection rule 2 forbids string ordering — "25.10" is a LATER line than
// "25.3", and sort.Strings put it first. (Found by the Python conformance
// driver's differential run: every peer SDK already sorted numerically.)
func sortMinorLines(lines []string) {
	sort.Slice(lines, func(i, j int) bool {
		return minorSortKey(lines[i]).before(minorSortKey(lines[j]))
	})
}

// minorSortKey is the numeric release order of one minor line, shared by every
// ordered surface this package exposes (Versions, Libraries, the not-found
// error's "have […]" text) so they can never disagree.
type minorKey [2]int

func (a minorKey) before(b minorKey) bool {
	if a[0] != b[0] {
		return a[0] < b[0]
	}
	return a[1] < b[1]
}

func minorSortKey(s string) minorKey {
	var k minorKey
	for i, p := range strings.SplitN(s, ".", 2) {
		n := 0
		for _, ch := range p {
			if ch < '0' || ch > '9' {
				n = -1
				break
			}
			n = n*10 + int(ch-'0')
		}
		k[i] = n
	}
	return k
}

func minorOf(v string) string {
	parts := strings.SplitN(v, ".", 3)
	if len(parts) < 2 {
		return v
	}
	return parts[0] + "." + parts[1]
}
