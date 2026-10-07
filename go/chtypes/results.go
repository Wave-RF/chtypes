package chtypes

// results.go — the result types (bindings-v1.md section 5). Every field is a
// field of the document the library returned, or the bytes of an output
// buffer; a decoder adds no field, computes no field and drops none it was
// asked to expose. A Go string is a byte string, so every name, rendering and
// message here is built from copied bytes and may hold NUL or invalid UTF-8.

// Span is a byte range: {off, len}.
type Span struct {
	Off uint64
	Len uint64
}

// Value is one entry of a row's columns.
type Value struct {
	// Column is the column's name, from name or name_b64.
	Column string
	// Text is ClickHouse's own rendering of the stored value, from stored.
	Text string
	// Value is the raw bytes of a scalar String or FixedString value, from
	// value_b64, as a byte string (bindings-v1.md section 3); nil otherwise
	// (absent for NULL and for a value nested in another type, a recorded 1.0
	// gap), so an absent value differs from an empty one. Text is always the
	// rendering.
	Value *string
	// Null is the library's own verdict, poison included.
	Null bool
	// Source is where the value came from, from src.
	Source Source
	// IsStored is the description's is_stored fact for Source.
	IsStored bool
}

// Transform is one silent change the library reports.
type Transform struct {
	Column string
	Input  string
	Stored string
	// Reason is a transform_reason value, and Lossy the description's own
	// lossy fact for it.
	Reason Reason
	Lossy  bool
	// Row is the row's index in the body, 0 in a single-row result.
	Row int
}

// Computed is a value the server computes for a column, such as a
// MATERIALIZED one.
type Computed struct {
	Column string
	Kind   string
	Text   string
	// Value is the raw bytes, as Value.Value.
	Value *string
}

// EngineCell is one cell of a stored row after the engine's insert-time merge.
type EngineCell struct {
	Column string
	Text   string
	Null   bool
	// Value is the raw bytes, as Value.Value.
	Value *string
}

// RowResult is one row's verdict and, as the flags ask, its columns.
type RowResult struct {
	// Outcome is final: the library applies every promotion.
	Outcome Outcome
	ErrCode int32
	ErrMsg  string
	// Columns is every entry of cols, in document order; Values is the subset
	// whose IsStored is true.
	Columns             []Value
	Values              []Value
	Transformed         []Transform
	UnknownFields       []string
	UnsupportedSettings []string
	Computed            []Computed
	// Verdict is present only with an attached filter.
	Verdict     *Verdict
	VerdictCode int32
	VerdictErr  string
	// PartitionID is nil when the document carries none; it is bytes.
	PartitionID *string
	// InputSpan is the bytes the reader consumed for this record.
	InputSpan *Span
}

// Header is the header lines the reader consumed.
type Header struct {
	Consumed bool
	Lines    int
	Names    []string
}

// Framing is what the reader decided about the body's framing. Unknown is
// never false and never empty: BomSkipped and Header are nil when the vendored
// reader does not expose the fact (the document's JSON null).
type Framing struct {
	BomSkipped *bool
	// Container is "array", "stream", or "" for none.
	Container string
	Header    *Header
}

// BatchResult is a body's verdict, counts and per-row documents.
type BatchResult struct {
	Outcome     Outcome
	ErrCode     int32
	ErrMsg      string
	Rows        []RowResult
	RowsRead    uint64
	RowsSkipped uint64
	// Transformed is every transform in the batch with its Row, as the
	// library lists them.
	Transformed []Transform
	// EngineRows is each stored row after the engine's insert-time merge, a
	// list of cells; nil when the document carries none.
	EngineRows [][]EngineCell
	// Payload is the export buffer: nil when no export was asked for or it was
	// declined, non-nil and empty for an accepted batch with zero rows.
	Payload []byte
	// Spans is each exported row's place in Payload; nil when absent.
	Spans          []Span
	ExportDeclined string
	RowsPassed     uint64
	RowsCut        uint64
	PartitionCount *uint64
	// Unconsumed is the byte ranges the reader's error recovery skipped. It
	// does not account for every record: a skipped row's InputSpan can cover
	// more than one input record, so verdicts can be fewer than records while
	// this is empty, and an independent record count can be fooled too. A
	// caller that needs every record accounted for declines a body with any
	// skipped row or any Unconsumed range (docs/guides/batches.md).
	Unconsumed []Span
	Framing    *Framing
}

// FilterRowError is one itemized row error of a filter evaluation.
type FilterRowError struct {
	Row  int
	Code int32
	Msg  string
}

// FilterResult is a filter evaluation.
type FilterResult struct {
	Outcome             FilterOutcome
	ErrCode             int32
	ErrMsg              string
	RowsRead            uint64
	UnsupportedSettings []string
	// Verdicts is one verdict per row. A caller enforcing visibility fails
	// closed on every verdict whose Answered is false.
	Verdicts []Verdict
	Errors   []FilterRowError
}

// Column is one column of a schema's description.
type Column struct {
	Name string
	// Type is the canonical type. It can differ between ClickHouse lines, so a
	// cross-line schema hash is taken over the caller's own statement.
	Type        string
	DefaultKind DefaultKind
	DefaultExpr string
}

// SchemaDescription is a schema's columns, in declared order, and the server
// it was compiled on.
type SchemaDescription struct {
	Columns []Column
	// Server is the server the schema was compiled on, from server: nil
	// exactly when it was compiled without one.
	Server *SchemaServer
	// Replicated is the ZooKeeper path and replica name of a Replicated
	// engine on a server, from replicated: nil when the document carries none.
	Replicated *SchemaReplicated
}

// SchemaServer is the server a schema was compiled on, as the library holds
// it. Its strings are the caller's own profile values given back, so they are
// plain text, not data-derived.
type SchemaServer struct {
	// Timezone is the zone the schema's types bind: the profile's, or else the
	// image zone (Setup's), exactly as chs_initialize spelled it.
	Timezone string
	// Settings is the server's settings as the profile gave them; the library
	// reports {} (an empty map) when it gave none.
	Settings map[string]string
	// Macros is the server's macro set: nil exactly when the profile carried
	// no macros (unknown), and non-nil, even empty, when it did (the complete
	// set).
	Macros map[string]string
}

// SchemaReplicated is what ClickHouse's own TableZnodeInfo resolved for a
// Replicated engine, fully expanded, to compare with the server's own
// system.replicas. Both expand DDL bytes, so both are byte strings.
type SchemaReplicated struct {
	ZooKeeperPath string
	ReplicaName   string
}

// DiscoveredColumn is one column read from a server's system.columns rows.
type DiscoveredColumn struct {
	Name string
	// Declaration is the column declaration as ClickHouse's own formatter
	// writes it.
	Declaration string
}

// Discovery is the columns of a server's table, in system.columns position
// order.
type Discovery struct {
	Columns []DiscoveredColumn
	// ColumnsSQL is the declarations joined for a CREATE TABLE.
	ColumnsSQL string
}

// Capabilities is what a build supports, from its own build_info.
type Capabilities struct {
	// InputFormats and ExportFormats are ClickHouse format names, from this
	// build's own format factory; compare with Format.CHName.
	InputFormats  []string
	ExportFormats []string
	DocFlags      []string
	// Features is open: an entry is a build-level feature, the first being
	// "default_generators".
	Features []string
}

// BuildInfo is the library's own account of itself, from chs_build_info.
type BuildInfo struct {
	Schema            int
	ABI               int
	ABIFingerprint    string
	ClickHouseVersion string
	Channel           string
	ClickHouseMinor   string
	ClickHouseCommit  string
	CoreCommit        string
	Build             string
	InputsSHA256      string
	OS                string
	Arch              string
	// Toolchain is the parsed object, uninterpreted.
	Toolchain    map[string]any
	Capabilities Capabilities
	// Raw is the exact bytes the library returned.
	Raw []byte
}

// ErrorCodeEntry is one code of this build's error-code table.
type ErrorCodeEntry struct {
	Code int32
	Name string
}

// ErrorCodeTable is this build's own error-code table, from chs_error_codes.
type ErrorCodeTable struct {
	entries []ErrorCodeEntry
	byCode  map[int32]string
	byName  map[string]int32
}

// Name is the name of a code. An unknown code is absent, never synthesized.
func (t *ErrorCodeTable) Name(code int32) (string, bool) {
	n, ok := t.byCode[code]
	return n, ok
}

// Code is the code of a name; names match exactly. An unknown name is absent.
func (t *ErrorCodeTable) Code(name string) (int32, bool) {
	c, ok := t.byName[name]
	return c, ok
}

// All is every entry, in ascending code order.
func (t *ErrorCodeTable) All() []ErrorCodeEntry {
	return append([]ErrorCodeEntry(nil), t.entries...)
}
