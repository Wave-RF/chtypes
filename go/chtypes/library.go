package chtypes

// library.go — one loaded image and what it can be asked (bindings-v1.md
// section 2, "The library"). A Library is never unloaded; every method makes
// exactly one ABI call and takes no lock.

import (
	"path/filepath"
	"sync"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
)

// Library is one loaded ClickHouse version, safe for concurrent use: every
// method may run concurrently. There is no Close: no binding ever unloads an
// image.
type Library struct {
	// Version is the build's clickhouse_version and Minor its
	// clickhouse_minor, both read from build_info, never derived. Path is the
	// loaded file.
	Version string
	Minor   string
	Path    string

	tbl      *abi2.Table
	info     BuildInfo
	resolved *Resolved

	codesMu sync.Mutex
	codes   *ErrorCodeTable
}

var images struct {
	mu sync.Mutex
	m  map[string]*Library
}

// imageKey separates verified opens from unverified ones, so a path opened
// without verification can never answer a verified request.
func imageKey(kind, path string) string {
	if abs, err := filepath.Abs(path); err == nil {
		path = abs
	}
	return kind + ":" + path
}

// openImage loads (or returns the already loaded) image for key. Every image is
// set up at load step 7, once, under the process setup, which the first open
// records. images.mu is held from the commit to the latch, so an image latches
// the record it loaded under, and Setup cannot change that record in between
// (it refuses a different setup while one is recorded). A failed load is never
// cached, so the next open runs every step again; the open that called this
// settles a failure with failedOpen, whatever failed in the attempt.
func openImage(key string, load func(zone, defaults []byte) (*abi2.Table, error), path string, resolved *Resolved) (*Library, error) {
	images.mu.Lock()
	defer images.mu.Unlock()
	zone, defaults, err := commitSetup()
	if err != nil {
		return nil, err
	}
	if l := images.m[key]; l != nil {
		return l, nil
	}
	tbl, err := load(zone, defaults)
	if err != nil {
		return nil, loadError(err)
	}
	latchSetup()
	info, err := decodeBuildInfo([]byte(tbl.BuildInfo()))
	if err != nil {
		return nil, &ArtifactError{
			Code: CodeArtifactCorrupt, Reason: "build_info_malformed", Path: path, Err: err,
			Msg: "chtypes: " + path + ": build_info does not decode [" + string(CodeArtifactCorrupt) + "]",
		}
	}
	l := &Library{
		Version: info.ClickHouseVersion, Minor: info.ClickHouseMinor, Path: path,
		tbl: tbl, info: info, resolved: resolved,
	}
	if images.m == nil {
		images.m = map[string]*Library{}
	}
	images.m[key] = l
	return l, nil
}

// OpenUnverified opens a local build with no signed statement: loader steps 1
// and 5 are skipped. It refuses with a *UsageError unless allow is true AND
// CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1 is set, and warns once per path. It runs
// step 7 under the process setup like any open, is not reachable through a
// Registry, and its Library has no Resolved record.
func OpenUnverified(path string, allow bool) (l *Library, err error) {
	// The caller's two opt-ins are checked before anything is attempted, so a
	// refusal here is misuse and unlocks nothing (bindings-v1.md section 6).
	if err := abi2.CheckUnverifiedAllowed(path, allow); err != nil {
		return nil, loadError(err)
	}
	gen := setupGeneration()
	defer func() {
		if err != nil {
			failedOpen(gen)
		}
	}()
	return openImage(imageKey("unverified", path), func(zone, defaults []byte) (*abi2.Table, error) {
		return abi2.OpenUnverified(path, allow, zone, defaults)
	}, path, nil)
}

// BuildInfo is the library's own account of itself, read once at load.
func (l *Library) BuildInfo() BuildInfo { return l.info }

// Resolved is the fetch record this library was opened by: nil when it was
// opened unverified or linked. When several registries open the same file it
// is the record that first opened it.
func (l *Library) Resolved() *Resolved { return l.resolved }

// text runs one call that returns a byte string.
func (l *Library) text(call func() (*abi2.Buf, *abi2.CallError)) (string, error) {
	buf, cerr := call()
	if cerr != nil {
		return "", callError(cerr)
	}
	return string(l.tbl.Take(buf)), nil
}

// document runs one call that returns a JSON document.
func (l *Library) document(call func() (*abi2.Buf, *abi2.CallError)) ([]byte, error) {
	buf, cerr := call()
	if cerr != nil {
		return nil, callError(cerr)
	}
	return l.tbl.Take(buf), nil
}

// ValidateType canonicalizes a type with ClickHouse's own parser
// (chs_type_validate).
func (l *Library) ValidateType(typeExpr string) (string, error) {
	return l.text(func() (*abi2.Buf, *abi2.CallError) { return l.tbl.TypeValidate([]byte(typeExpr)) })
}

// QuoteIdentifier quotes an identifier, always (chs_back_quote).
func (l *Library) QuoteIdentifier(name string) (string, error) {
	return l.text(func() (*abi2.Buf, *abi2.CallError) { return l.tbl.BackQuote([]byte(name)) })
}

// QuoteIdentifierIfNeeded quotes an identifier only when it needs it
// (chs_back_quote_if_needed).
func (l *Library) QuoteIdentifierIfNeeded(name string) (string, error) {
	return l.text(func() (*abi2.Buf, *abi2.CallError) { return l.tbl.BackQuoteIfNeeded([]byte(name)) })
}

// QuoteLiteral quotes a string literal (chs_quote_string).
func (l *Library) QuoteLiteral(text string) (string, error) {
	return l.text(func() (*abi2.Buf, *abi2.CallError) { return l.tbl.QuoteString([]byte(text)) })
}

// ErrorCodes is this build's own error-code table (chs_error_codes). It is
// built on the first successful call and kept for the library's life; a failure
// is never cached.
func (l *Library) ErrorCodes() (*ErrorCodeTable, error) {
	l.codesMu.Lock()
	defer l.codesMu.Unlock()
	if l.codes != nil {
		return l.codes, nil
	}
	raw, err := l.document(l.tbl.ErrorCodes)
	if err != nil {
		return nil, err
	}
	t, err := decodeErrorCodes(raw)
	if err != nil {
		return nil, err
	}
	l.codes = t
	return t, nil
}

// DiscoverQuery is the query a caller runs against its server to read a
// table's system.columns rows (chs_discover_query): FORMAT JSONEachRow, with
// the ClickHouse query parameters {database:String} and {table:String}. The
// caller binds them its own way; no binding builds SQL.
func (l *Library) DiscoverQuery() (string, error) {
	return l.text(l.tbl.DiscoverQuery)
}

// DiscoverColumns reads a server's system.columns rows, the JSONEachRow answer
// to DiscoverQuery, and returns the column declarations as ClickHouse's own
// formatter writes them (chs_discover_columns).
func (l *Library) DiscoverColumns(rows []byte) (Discovery, error) {
	raw, err := l.document(func() (*abi2.Buf, *abi2.CallError) { return l.tbl.DiscoverColumns(rows) })
	if err != nil {
		return Discovery{}, err
	}
	return decodeDiscovery(raw)
}

// LiveHandles is the count of live handles of each kind in this image
// (chs_live_handles): a diagnostic, and what the test suites prove their
// finalizers with.
func (l *Library) LiveHandles() (map[string]uint64, error) {
	raw, err := l.document(l.tbl.LiveHandles)
	if err != nil {
		return nil, err
	}
	return decodeLiveHandles(raw)
}

// CompileTable compiles exactly one CREATE TABLE statement (chs_schema_create).
func (l *Library) CompileTable(createTable string, opts ...CompileOption) (*Schema, error) {
	c := compileConfig(opts)
	settings, err := callSettings(c.settings, c.timezone)
	if err != nil {
		return nil, err
	}
	// No server (NULL) and no options (length 0, `{}`): the schema is on the
	// image's own server, exactly as before the server profile existed.
	h, cerr := l.tbl.SchemaCreate(nil, []byte(createTable), settings, nil)
	if cerr != nil {
		return nil, callError(cerr)
	}
	return newSchema(l, h), nil
}
