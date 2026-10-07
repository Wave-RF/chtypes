package chtypes

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

// ColumnsOption is an option that applies to the three calls that parse an
// INSERT body.
type ColumnsOption interface {
	RowOption
	RowsOption
	BlockOption
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

func (o columnsOpt) apply(c *callConfig)     { c.columns, c.haveColumns = o, true }
func (o columnsOpt) applyRow(c *callConfig)  { o.apply(c) }
func (o columnsOpt) applyRows(c *callConfig) { o.apply(c) }
func (o columnsOpt) applyBlock(c *callConfig) {
	o.apply(c)
}

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

// WithColumns sets the INSERT column list.
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
