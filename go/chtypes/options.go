package chtypes

import (
	"encoding/base64"
	"unicode/utf8"
)

// options.go — the call options (bindings-v1.md section 2, "The call options").
// One entry point per operation, with its optional inputs as Go functional
// options. Each operation takes its own option interface, and an option
// implements exactly the interfaces of the operations it applies to, so
// passing one to a call it does not belong to is a compile error.

type callConfig struct {
	settings    map[string]string
	timezone    *string
	columns     []string
	haveColumns bool
	filter      *Filter
	export      Format
	docFlags    DocFlags
	params      map[string]string
	server      *Server
	// The three options documents (rule r1), built member by member.
	schemaOpts, insertOpts, filterOpts optionDoc
}

func newCallConfig() *callConfig {
	return &callConfig{export: ExportNone, docFlags: DocAll}
}

// CompileOption configures CompileTable.
type CompileOption interface{ applyCompile(*callConfig) }

// RowOption configures Schema.Row.
type RowOption interface{ applyRow(*callConfig) }

// RowsOption configures Schema.Rows.
type RowsOption interface{ applyRows(*callConfig) }

// BlockOption configures Schema.ParseBlock.
type BlockOption interface{ applyBlock(*callConfig) }

// FilterOption configures Schema.CompileFilter.
type FilterOption interface{ applyFilter(*callConfig) }

// EvalOption configures Filter.Rows.
type EvalOption interface{ applyEval(*callConfig) }

// SettingsOption is an option that applies to all six operations: the
// settings and the per-call zone.
type SettingsOption interface {
	CompileOption
	RowOption
	RowsOption
	BlockOption
	FilterOption
	EvalOption
}

// ColumnsOption is an option that applies to the four calls that model an
// INSERT of a body: Row, Rows, ParseBlock and Filter.Rows.
type ColumnsOption interface {
	RowOption
	RowsOption
	BlockOption
	EvalOption
}

// InsertOption is an option of the insert_options document: it applies to
// the four calls that model an INSERT of a body, Row, Rows, ParseBlock and
// Filter.Rows, and means the same at each.
type InsertOption interface {
	RowOption
	RowsOption
	BlockOption
	EvalOption
}

// The options are small named types, one per option, rather than closures: a
// closure is a heap allocation each time an option is built, and a map, a
// pointer or a small integer converts to an interface without one. Each type
// implements exactly the option interfaces of the operations it applies to,
// and the exported constructors return only those interfaces.

type settingsOpt map[string]string

func (o settingsOpt) apply(c *callConfig)        { c.settings = o }
func (o settingsOpt) applyCompile(c *callConfig) { o.apply(c) }
func (o settingsOpt) applyRow(c *callConfig)     { o.apply(c) }
func (o settingsOpt) applyRows(c *callConfig)    { o.apply(c) }
func (o settingsOpt) applyBlock(c *callConfig)   { o.apply(c) }
func (o settingsOpt) applyFilter(c *callConfig)  { o.apply(c) }
func (o settingsOpt) applyEval(c *callConfig)    { o.apply(c) }

type timezoneOpt string

func (o timezoneOpt) apply(c *callConfig) {
	name := string(o)
	c.timezone = &name
}
func (o timezoneOpt) applyCompile(c *callConfig) { o.apply(c) }
func (o timezoneOpt) applyRow(c *callConfig)     { o.apply(c) }
func (o timezoneOpt) applyRows(c *callConfig)    { o.apply(c) }
func (o timezoneOpt) applyBlock(c *callConfig)   { o.apply(c) }
func (o timezoneOpt) applyFilter(c *callConfig)  { o.apply(c) }
func (o timezoneOpt) applyEval(c *callConfig)    { o.apply(c) }

type columnsOpt []string

func (o columnsOpt) apply(c *callConfig)      { c.columns, c.haveColumns = o, true }
func (o columnsOpt) applyRow(c *callConfig)   { o.apply(c) }
func (o columnsOpt) applyRows(c *callConfig)  { o.apply(c) }
func (o columnsOpt) applyBlock(c *callConfig) { o.apply(c) }
func (o columnsOpt) applyEval(c *callConfig)  { o.apply(c) }

type rowFilterOpt struct{ f *Filter }

func (o rowFilterOpt) applyRows(c *callConfig) { c.filter = o.f }

type exportOpt Format

func (o exportOpt) applyRows(c *callConfig) { c.export = Format(o) }

type docFlagsOpt DocFlags

func (o docFlagsOpt) applyRows(c *callConfig) { c.docFlags = DocFlags(o) }

type paramsOpt map[string]string

func (o paramsOpt) applyFilter(c *callConfig) { c.params = o }

type serverOpt struct{ s *Server }

func (o serverOpt) applyCompile(c *callConfig) { c.server = o.s }

// WithSettings sets the call's settings: a map of strings to strings,
// serialized as a JSON object and passed verbatim. No value is ever rewritten.
// On a compile it is the profile; on a filter compile the filter's own profile;
// on the calls that parse a body, the body's parse settings.
func WithSettings(settings map[string]string) SettingsOption {
	return settingsOpt(settings)
}

// WithSessionTimezone sets the per-call zone: the session_timezone key of the
// call's settings, written verbatim and never validated here (the library
// validates it). Passing it together with a session_timezone settings key is a
// *UsageError, whether or not the two agree.
func WithSessionTimezone(name string) SettingsOption {
	return timezoneOpt(name)
}

// WithColumns sets the INSERT column list. It applies to every call that models
// an INSERT of a body, Filter.Rows included.
func WithColumns(names []string) ColumnsOption {
	return columnsOpt(names)
}

// WithRowFilter attaches a compiled filter to a Rows call.
func WithRowFilter(f *Filter) RowsOption {
	return rowFilterOpt{f}
}

// WithExport asks Rows to also serialize the accepted rows in the given
// format; absent means ExportNone.
func WithExport(format Format) RowsOption {
	return exportOpt(format)
}

// WithDocFlags chooses the document groups Rows returns; absent means DocAll.
func WithDocFlags(flags DocFlags) RowsOption {
	return docFlagsOpt(flags)
}

// WithFilterParams sets the query parameters of a filter compile.
func WithFilterParams(params map[string]string) FilterOption {
	return paramsOpt(params)
}

// OnServer compiles the table on a server made by Library.NewServer
// (chs_schema_create's server): the schema's home zone is the server's
// timezone, the server's settings layer under the schema's own, and a
// Replicated engine's ZooKeeper path and replica name expand the server's
// macros. A closed server is a *UsageError, raised before any call; a server
// from another Library is that library's own refusal, also a *UsageError.
// OnServer(nil) is no server, the same as leaving the option out: the
// schema is on the image's own server.
//
//	srv, err := lib.NewServer(chtypes.ServerProfile{Timezone: "Asia/Tokyo"})
//	schema, err := lib.CompileTable(stmt, chtypes.OnServer(srv), chtypes.WithSettings(settings))
func OnServer(s *Server) CompileOption {
	return serverOpt{s}
}

func compileConfig(opts []CompileOption) *callConfig {
	c := newCallConfig()
	for _, o := range opts {
		if o != nil {
			o.applyCompile(c)
		}
	}
	return c
}

func rowConfig(opts []RowOption) *callConfig {
	c := newCallConfig()
	for _, o := range opts {
		if o != nil {
			o.applyRow(c)
		}
	}
	return c
}

func rowsConfig(opts []RowsOption) *callConfig {
	c := newCallConfig()
	for _, o := range opts {
		if o != nil {
			o.applyRows(c)
		}
	}
	return c
}

func blockConfig(opts []BlockOption) *callConfig {
	c := newCallConfig()
	for _, o := range opts {
		if o != nil {
			o.applyBlock(c)
		}
	}
	return c
}

func filterConfig(opts []FilterOption) *callConfig {
	c := newCallConfig()
	for _, o := range opts {
		if o != nil {
			o.applyFilter(c)
		}
	}
	return c
}

func evalConfig(opts []EvalOption) *callConfig {
	c := newCallConfig()
	for _, o := range opts {
		if o != nil {
			o.applyEval(c)
		}
	}
	return c
}

// ---- the options documents (rule r1). Each member is its own option, and a
// key the binding has no name for goes through WithInsertOption, WithFilterOption
// or WithSchemaOption as raw JSON. The binding builds the document from the
// members the caller set, sends nothing when none was set, and never validates a
// member: the library refuses a key it does not know, or a value that is not one
// of the member's, with CHS_INVALID_ARGUMENT, naming it.

// VerdictBasis is insert_options' verdict_basis: what row a filter verdict answers
// for.
type VerdictBasis string

const (
	// VerdictBasisInput is the default: the verdict answers for the input row.
	VerdictBasisInput VerdictBasis = "input"
	// VerdictBasisExport answers for the row the export would store. A build
	// that does not implement it declines it.
	VerdictBasisExport VerdictBasis = "export"
)

// DeferDefaults is insert_options' defer_defaults.
type DeferDefaults string

const (
	// DeferDefaultsNone is the default: no DEFAULT is deferred.
	DeferDefaultsNone DeferDefaults = "none"
	// DeferDefaultsServer defers to the server each DEFAULT the build would
	// otherwise decline. A build that does not implement it declines it.
	DeferDefaultsServer DeferDefaults = "server"
)

// ParamEncoding is filter_options' param_encoding: how a query parameter's value
// is read.
type ParamEncoding string

const (
	// ParamEncodingEscaped is the default: ClickHouse's escaped-text reader.
	ParamEncodingEscaped ParamEncoding = "escaped"
	// ParamEncodingRaw takes the bytes as the value. A build that does not
	// implement it declines it.
	ParamEncodingRaw ParamEncoding = "raw"
)

// ColumnDefault is one entry of insert_options' column_defaults: the value the
// call uses in a cell the server would fill from the column's DEFAULT. Both
// fields are byte strings: a Name or Value that is not valid UTF-8 is sent in
// its _b64 form.
type ColumnDefault struct {
	Name  string
	Value string
}

type insertableColumnsOpt []string

func (o insertableColumnsOpt) apply(c *callConfig) {
	c.insertOpts.set("insertable_columns", nameObjects(o))
}
func (o insertableColumnsOpt) applyRow(c *callConfig)   { o.apply(c) }
func (o insertableColumnsOpt) applyRows(c *callConfig)  { o.apply(c) }
func (o insertableColumnsOpt) applyBlock(c *callConfig) { o.apply(c) }
func (o insertableColumnsOpt) applyEval(c *callConfig)  { o.apply(c) }

type columnDefaultsOpt []ColumnDefault

func (o columnDefaultsOpt) apply(c *callConfig) {
	list := make([]map[string]string, 0, len(o))
	for _, d := range o {
		e := nameObjects([]string{d.Name})[0]
		if utf8.ValidString(d.Value) {
			e["value"] = d.Value
		} else {
			e["value_b64"] = base64.StdEncoding.EncodeToString([]byte(d.Value))
		}
		list = append(list, e)
	}
	c.insertOpts.set("column_defaults", list)
}
func (o columnDefaultsOpt) applyRow(c *callConfig)   { o.apply(c) }
func (o columnDefaultsOpt) applyRows(c *callConfig)  { o.apply(c) }
func (o columnDefaultsOpt) applyBlock(c *callConfig) { o.apply(c) }
func (o columnDefaultsOpt) applyEval(c *callConfig)  { o.apply(c) }

// insertMember is any other member of insert_options: a key and its value.
type insertMember struct {
	key string
	v   any
}

func (o insertMember) apply(c *callConfig) {
	if o.key != "" { // the zero insertMember is an option that sets nothing
		c.insertOpts.set(o.key, o.v)
	}
}
func (o insertMember) applyRow(c *callConfig)   { o.apply(c) }
func (o insertMember) applyRows(c *callConfig)  { o.apply(c) }
func (o insertMember) applyBlock(c *callConfig) { o.apply(c) }
func (o insertMember) applyEval(c *callConfig)  { o.apply(c) }

type filterMember struct {
	key string
	v   any
}

func (o filterMember) applyFilter(c *callConfig) { c.filterOpts.set(o.key, o.v) }

type schemaMember struct {
	key string
	v   any
}

func (o schemaMember) applyCompile(c *callConfig) { c.schemaOpts.set(o.key, o.v) }

// WithInsertableColumns sets insert_options' insertable_columns: the columns an
// INSERT may supply (the server's column-level INSERT grant), checked on the
// INSERT's header before any byte of the body is read. nil leaves the member out
// (every column); a non-nil empty slice is sent as [] (no column).
func WithInsertableColumns(names []string) InsertOption {
	if names == nil {
		return insertMember{}
	}
	return insertableColumnsOpt(names)
}

// WithColumnDefaults sets insert_options' column_defaults: per-call DEFAULT
// overrides. nil leaves the member out; a non-nil empty slice is sent as [].
func WithColumnDefaults(defaults []ColumnDefault) InsertOption {
	if defaults == nil {
		return insertMember{}
	}
	return columnDefaultsOpt(defaults)
}

// WithVerdictBasis sets insert_options' verdict_basis.
func WithVerdictBasis(b VerdictBasis) InsertOption {
	return insertMember{"verdict_basis", string(b)}
}

// WithDeferDefaults sets insert_options' defer_defaults.
func WithDeferDefaults(d DeferDefaults) InsertOption {
	return insertMember{"defer_defaults", string(d)}
}

// WithInsertOption sets a member of insert_options this binding has no name
// for, with its value as JSON text, passed through as given. The library
// refuses a key it does not know (CHS_INVALID_ARGUMENT, naming it); a value that
// is not valid JSON is a *UsageError raised before any call.
func WithInsertOption(key string, valueJSON []byte) InsertOption {
	return insertMember{key, rawMember(valueJSON)}
}

// WithParamEncoding sets filter_options' param_encoding.
func WithParamEncoding(e ParamEncoding) FilterOption {
	return filterMember{"param_encoding", string(e)}
}

// WithFilterOption sets a member of filter_options this binding has no name
// for, as WithInsertOption does for insert_options.
func WithFilterOption(key string, valueJSON []byte) FilterOption {
	return filterMember{key, rawMember(valueJSON)}
}

// WithPosture sets schema_options' posture: how the statement is compiled,
// PostureCreate (the default) as a server's CREATE TABLE, or PostureLoad as a
// server loads the stored definition of a table it holds. The caller's
// obligation under PostureLoad is spec/abi-v2/docs.md's (input:schema_options).
func WithPosture(p Posture) CompileOption {
	return schemaMember{"posture", string(p)}
}

// WithSchemaOption sets a member of schema_options this binding has no name
// for, as WithInsertOption does for insert_options.
func WithSchemaOption(key string, valueJSON []byte) CompileOption {
	return schemaMember{key, rawMember(valueJSON)}
}
