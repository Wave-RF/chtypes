package chtypes

// The error-code table (revision 6): ClickHouse's own code -> name mapping,
// answered by ONE loaded library through chs_error_codes and nothing else.
//
// There is no table in this package, and there must never be one. The table
// is a property of the BUILD: codes join and leave between lines, and one
// number can name two different errors on two lines (903 is LICENSE_EXPIRED
// on 25.3 and 25.8, absent on 25.10, and DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN
// on 26.2 through 26.9). A table written down here would be right for at most one line and silently wrong
// for the rest, which is why every answer hangs off a *Library and why
// scripts/check-no-error-code-table.py fails the build if a literal code ->
// name table appears in any binding.

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"sync"
)

// ErrorCodeEntry is one row of a build's error-code table: a ClickHouse error
// code and the name THAT BUILD gives it.
type ErrorCodeEntry struct {
	Code int
	Name string
}

// ErrorCodeTable is one loaded library's own error-code table, from
// chs_error_codes — obtained from (*Library).ErrorCodes and valid for that
// library's ClickHouse line only. Read-only once built, so it is safe for
// concurrent use.
//
// Lookups answer only what the build's own table holds: an unknown code, a
// negative code (including the ABI's -1 and -2 sentinels, which are not
// ClickHouse codes), or an unknown name is absent, never a synthesized
// spelling. Names match exactly and case-sensitively, as the server prints
// them.
type ErrorCodeTable struct {
	entries []ErrorCodeEntry // ascending by Code
	byCode  map[int]string
	byName  map[string]int
}

// Name is the name this build gives code, and whether it has one.
func (t *ErrorCodeTable) Name(code int) (string, bool) {
	if t == nil || code < 0 {
		return "", false
	}
	name, ok := t.byCode[code]
	return name, ok
}

// Code is the code this build gives name, and whether it has one. The match
// is exact and case-sensitive.
func (t *ErrorCodeTable) Code(name string) (int, bool) {
	if t == nil {
		return 0, false
	}
	code, ok := t.byName[name]
	return code, ok
}

// All is every entry, in ascending code order. The slice is a copy; changing
// it changes nothing in the table.
func (t *ErrorCodeTable) All() []ErrorCodeEntry {
	if t == nil {
		return nil
	}
	return append([]ErrorCodeEntry(nil), t.entries...)
}

// errorCodesDoc mirrors the document chs_error_codes returns. Unknown keys are
// ignored and an absent (or null) key is its zero value, like every document
// this ABI hands back. The entries stay raw until each is checked to be an
// object: encoding/json would read a null entry as a zero-valued one, and a
// null document as an empty table, which no other binding accepts.
type errorCodesDoc struct {
	ErrorCodes []json.RawMessage `json:"error_codes"`
}

type errorCodeEntryDoc struct {
	Code int    `json:"code"`
	Name string `json:"name"`
}

// isJSONObject reports whether raw is a JSON object — its first byte past
// JSON's own whitespace is '{'. A syntax error inside is json.Unmarshal's to
// find; this decides only the SHAPE a document or an entry must have.
func isJSONObject(raw []byte) bool {
	for _, b := range raw {
		switch b {
		case ' ', '\t', '\n', '\r':
			continue
		}
		return b == '{'
	}
	return false
}

// errorCodeTableOf builds a table from a chs_error_codes document. It keeps
// the library's entries and nothing else: an entry with an empty name is not
// a name and is dropped, a negative code is not a ClickHouse code and is
// dropped, and where the document repeats a code or a name the first entry
// wins. The entries are ordered by code so All's contract holds whatever
// order they arrived in.
func errorCodeTableOf(doc []byte) (*ErrorCodeTable, error) {
	var d errorCodesDoc
	if err := json.Unmarshal(doc, &d); err != nil {
		return nil, fmt.Errorf("chtypes: bad chs_error_codes document: %w", err)
	}
	if !isJSONObject(doc) {
		return nil, errors.New("chtypes: bad chs_error_codes document: not a JSON object")
	}
	t := &ErrorCodeTable{byCode: map[int]string{}, byName: map[string]int{}}
	for _, raw := range d.ErrorCodes {
		if !isJSONObject(raw) {
			return nil, fmt.Errorf("chtypes: bad chs_error_codes document: an entry is not an object: %s", raw)
		}
		var e errorCodeEntryDoc
		if err := json.Unmarshal(raw, &e); err != nil {
			return nil, fmt.Errorf("chtypes: bad chs_error_codes document: entry %s: %w", raw, err)
		}
		if e.Name == "" || e.Code < 0 {
			continue
		}
		if _, dup := t.byCode[e.Code]; dup {
			continue
		}
		if _, dup := t.byName[e.Name]; dup {
			continue
		}
		t.byCode[e.Code] = e.Name
		t.byName[e.Name] = e.Code
		t.entries = append(t.entries, ErrorCodeEntry(e))
	}
	sort.SliceStable(t.entries, func(i, j int) bool { return t.entries[i].Code < t.entries[j].Code })
	return t, nil
}

// errorCodeCache holds one library's table once it has been built, and only
// then. A NULL answer from chs_error_codes is a guarded exception inside the
// library — transient by definition — so it is returned as an error and NOT
// remembered: the next call asks again. Only a table that was actually built
// is kept.
type errorCodeCache struct {
	mu    sync.Mutex
	table *ErrorCodeTable
}

// get answers the cached table, or runs fetch and keeps its table if (and
// only if) it produced one.
func (c *errorCodeCache) get(fetch func() (*ErrorCodeTable, error)) (*ErrorCodeTable, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.table != nil {
		return c.table, nil
	}
	t, err := fetch()
	if err != nil {
		return nil, err
	}
	c.table = t
	return t, nil
}

// errNoErrorCodesDocument is the error for chs_error_codes answering NULL: the
// library could not build the document (a guarded exception). It is neither a
// refusal nor a decline, so it is a plain error, and it is never cached.
var errNoErrorCodesDocument = errors.New("chtypes: chs_error_codes returned no document " +
	"(a guarded exception inside the library); nothing was cached, so the next call asks again")

// partitionByError maps chs_schema_partition_by's return code to an error, by
// chs_schema_engine's SIGN rule — NOT chs_schema_ttl's, which declines on
// every nonzero code. 0 is accepted; a positive code is the server's own
// CREATE-path refusal (e.g. 36 BAD_ARGUMENTS for a non-deterministic key, 549
// DATA_TYPE_CANNOT_BE_USED_IN_KEY), a *SchemaError carrying that code and
// message; any negative code — -2 a key the server accepts but this build
// will not evaluate, -1 a guarded exception, -3 this loader's "the artifact
// predates the symbol" — is an *UnsupportedError.
func partitionByError(rc int, msg string) error {
	if rc == 0 {
		return nil
	}
	if rc == -3 {
		msg = "this artifact predates chs_schema_partition_by (rebuild it)"
	}
	return schemaErr(rc, msg, "")
}
