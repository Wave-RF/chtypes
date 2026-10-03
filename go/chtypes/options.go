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

// option is one functional option; its methods make it every option
// interface, and the exported constructors return only the interface the
// option belongs to.
type option func(*callConfig)

func (o option) applyCompile(c *callConfig) { o(c) }
func (o option) applyRow(c *callConfig)     { o(c) }
func (o option) applyRows(c *callConfig)    { o(c) }
func (o option) applyBlock(c *callConfig)   { o(c) }
func (o option) applyFilter(c *callConfig)  { o(c) }
func (o option) applyEval(c *callConfig)    { o(c) }

// WithSettings sets the call's settings: a map of strings to strings,
// serialized as a JSON object and passed verbatim. No value is ever rewritten.
// On a compile it is the profile; on a filter compile the filter's own profile;
// on the calls that parse a body, the body's parse settings.
func WithSettings(settings map[string]string) SettingsOption {
	return option(func(c *callConfig) { c.settings = settings })
}

// WithSessionTimezone sets the per-call zone: the session_timezone key of the
// call's settings, written verbatim and never validated here (the library
// validates it). Passing it together with a session_timezone settings key is a
// *UsageError, whether or not the two agree.
func WithSessionTimezone(name string) SettingsOption {
	return option(func(c *callConfig) { c.timezone = &name })
}

// WithColumns sets the INSERT column list.
func WithColumns(names []string) ColumnsOption {
	return option(func(c *callConfig) { c.columns, c.haveColumns = names, true })
}

// WithRowFilter attaches a compiled filter to a Rows call.
func WithRowFilter(f *Filter) RowsOption {
	return option(func(c *callConfig) { c.filter = f })
}

// WithExport asks Rows to also serialize the accepted rows in the given
// format; absent means ExportNone.
func WithExport(format Format) RowsOption {
	return option(func(c *callConfig) { c.export = format })
}

// WithDocFlags chooses the document groups Rows returns; absent means DocAll.
func WithDocFlags(flags DocFlags) RowsOption {
	return option(func(c *callConfig) { c.docFlags = flags })
}

// WithFilterParams sets the query parameters of a filter compile.
func WithFilterParams(params map[string]string) FilterOption {
	return option(func(c *callConfig) { c.params = params })
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
