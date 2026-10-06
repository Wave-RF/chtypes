package chtypes

// schema.go — Schema, Filter and Block (bindings-v1.md sections 2 and 3). Each
// is safe for concurrent use: any number of goroutines may call one handle at
// once, and Close waits for the calls already inside that object (a
// per-object sync.RWMutex: calls read-lock, Close write-locks). The library
// makes every call on a compiled handle safe to run concurrently, so there is
// no lock around a call beyond this guard. Close is idempotent and any order
// is safe: a filter or a block holds a counted reference to its schema inside
// the library.

import (
	"runtime"
	"sync"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
)

// guard is the close guard of one handle object.
type guard struct {
	mu     sync.RWMutex
	closed bool
}

// enter takes the read side, or reports a closed object. Every enter that
// returns nil is paired with leave.
func (g *guard) enter(what string) error {
	g.mu.RLock()
	if g.closed {
		g.mu.RUnlock()
		return usageError("%s is closed", what)
	}
	return nil
}

func (g *guard) leave() { g.mu.RUnlock() }

// shut closes once, after the calls already inside have returned.
func (g *guard) shut(release func()) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !g.closed {
		g.closed = true
		release()
	}
}

// Schema is a compiled table. Close releases it; a finalizer frees what the
// caller abandons.
type Schema struct {
	g   guard
	lib *Library
	h   *abi2.Schema
}

func newSchema(l *Library, h *abi2.Schema) *Schema {
	s := &Schema{lib: l, h: h}
	runtime.SetFinalizer(s, func(s *Schema) { _ = s.Close() })
	return s
}

// Close releases the schema, after the calls already inside it have returned.
// It is idempotent. A later call is a *UsageError.
func (s *Schema) Close() error {
	if s == nil {
		return nil
	}
	s.g.shut(func() {
		runtime.SetFinalizer(s, nil)
		_ = s.h.Close()
	})
	return nil
}

func (s *Schema) enter() error {
	if s == nil {
		return usageError("the schema is nil")
	}
	return s.g.enter("the schema")
}

// Describe returns the schema's columns (chs_schema_describe).
func (s *Schema) Describe() (SchemaDescription, error) {
	if err := s.enter(); err != nil {
		return SchemaDescription{}, err
	}
	defer s.g.leave()
	raw, err := s.lib.document(func() (*abi2.Buf, *abi2.CallError) { return s.lib.tbl.SchemaDescribe(s.h) })
	if err != nil {
		return SchemaDescription{}, err
	}
	return decodeSchemaDescription(raw)
}

// Row previews one row (chs_preview_row).
func (s *Schema) Row(format Format, body []byte, opts ...RowOption) (RowResult, error) {
	c := rowConfig(opts)
	settings, columns, err := callInputs(c)
	if err != nil {
		return RowResult{}, err
	}
	if err := s.enter(); err != nil {
		return RowResult{}, err
	}
	defer s.g.leave()
	raw, err := s.lib.document(func() (*abi2.Buf, *abi2.CallError) {
		return s.lib.tbl.PreviewRow(s.h, int32(format), body, settings, columns)
	})
	if err != nil {
		return RowResult{}, err
	}
	return decodeRow(raw)
}

// Rows previews a whole body (chs_preview_batch), optionally with a filter
// attached and an export of the accepted rows.
func (s *Schema) Rows(format Format, body []byte, opts ...RowsOption) (BatchResult, error) {
	c := rowsConfig(opts)
	settings, columns, err := callInputs(c)
	if err != nil {
		return BatchResult{}, err
	}
	if err := s.enter(); err != nil {
		return BatchResult{}, err
	}
	defer s.g.leave()
	var fh *abi2.Filter
	if c.filter != nil {
		if err := c.filter.enter(); err != nil {
			return BatchResult{}, err
		}
		defer c.filter.g.leave()
		fh = c.filter.h
	}
	docBuf, exportBuf, cerr := s.lib.tbl.PreviewBatch(s.h, int32(format), body, settings, columns, fh, int32(c.export), uint32(c.docFlags))
	if cerr != nil {
		return BatchResult{}, callError(cerr)
	}
	// The document is read once, here, and nothing the result holds points
	// into it (decodeBatch copies every value out), so its copy lives in a
	// pooled buffer. The export payload is returned to the caller, so it is a
	// copy of its own.
	bp, _ := docBufs.Get().(*[]byte)
	if bp == nil {
		bp = new([]byte)
	}
	raw := s.lib.tbl.TakeInto(docBuf, *bp)
	payload := s.lib.tbl.Take(exportBuf)
	res, err := decodeBatch(raw, payload)
	if raw != nil && cap(raw) <= docBufMax {
		*bp = raw[:0]
	}
	docBufs.Put(bp)
	return res, err
}

// docBufs holds the document buffers of Rows between calls; one larger than
// docBufMax is left to the collector rather than kept.
var docBufs sync.Pool

const docBufMax = 1 << 20

// CompileFilter compiles a boolean expression over the schema (chs_filter_create).
// The filter's zone is its own, fixed here: the session timezone option, or a
// session_timezone settings key, else the schema's profile, the setup defaults
// and the image zone, in that order. Every evaluation of the filter runs its
// WHERE under that zone.
func (s *Schema) CompileFilter(expr string, opts ...FilterOption) (*Filter, error) {
	c := filterConfig(opts)
	settings, err := callSettings(c.settings, c.timezone)
	if err != nil {
		return nil, err
	}
	params, err := stringMapJSON(c.params)
	if err != nil {
		return nil, err
	}
	if err := s.enter(); err != nil {
		return nil, err
	}
	defer s.g.leave()
	h, cerr := s.lib.tbl.FilterCreate(s.h, []byte(expr), params, settings)
	if cerr != nil {
		return nil, callError(cerr)
	}
	return newFilter(s.lib, h), nil
}

// ParseBlock parses a body once (chs_block_create), so many filters can be
// evaluated over it with no re-parse.
func (s *Schema) ParseBlock(format Format, body []byte, opts ...BlockOption) (*Block, error) {
	c := blockConfig(opts)
	settings, columns, err := callInputs(c)
	if err != nil {
		return nil, err
	}
	if err := s.enter(); err != nil {
		return nil, err
	}
	defer s.g.leave()
	h, cerr := s.lib.tbl.BlockCreate(s.h, int32(format), body, settings, columns)
	if cerr != nil {
		return nil, callError(cerr)
	}
	return newBlock(s.lib, h), nil
}

// callInputs serializes the settings (the per-call zone written into them) and
// the column list of a call.
func callInputs(c *callConfig) (settings, columns []byte, err error) {
	if settings, err = callSettings(c.settings, c.timezone); err != nil {
		return nil, nil, err
	}
	if columns, err = columnsJSON(c.columns, c.haveColumns); err != nil {
		return nil, nil, err
	}
	return settings, columns, nil
}

// Filter is a compiled filter. It holds its schema inside the library.
type Filter struct {
	g   guard
	lib *Library
	h   *abi2.Filter
}

func newFilter(l *Library, h *abi2.Filter) *Filter {
	f := &Filter{lib: l, h: h}
	runtime.SetFinalizer(f, func(f *Filter) { _ = f.Close() })
	return f
}

// Close releases the filter, after the calls already inside it have returned.
// It is idempotent. A later call is a *UsageError.
func (f *Filter) Close() error {
	if f == nil {
		return nil
	}
	f.g.shut(func() {
		runtime.SetFinalizer(f, nil)
		_ = f.h.Close()
	})
	return nil
}

func (f *Filter) enter() error {
	if f == nil {
		return usageError("the filter is nil")
	}
	return f.g.enter("the filter")
}

// Rows evaluates the filter over a body (chs_filter_eval_body). The settings
// are the body's parse settings only; the filter's own zone governs its WHERE.
func (f *Filter) Rows(format Format, body []byte, opts ...EvalOption) (FilterResult, error) {
	c := evalConfig(opts)
	settings, err := callSettings(c.settings, c.timezone)
	if err != nil {
		return FilterResult{}, err
	}
	if err := f.enter(); err != nil {
		return FilterResult{}, err
	}
	defer f.g.leave()
	raw, err := f.lib.document(func() (*abi2.Buf, *abi2.CallError) {
		return f.lib.tbl.FilterEvalBody(f.h, int32(format), body, settings)
	})
	if err != nil {
		return FilterResult{}, err
	}
	return decodeFilterResult(raw)
}

// Eval evaluates the filter over a parsed block (chs_filter_eval_block). It
// takes no settings: the filter brings its zone, and the block brought its
// parse zone when it was parsed.
func (f *Filter) Eval(b *Block) (FilterResult, error) {
	if err := f.enter(); err != nil {
		return FilterResult{}, err
	}
	defer f.g.leave()
	if err := b.enter(); err != nil {
		return FilterResult{}, err
	}
	defer b.g.leave()
	raw, err := f.lib.document(func() (*abi2.Buf, *abi2.CallError) { return f.lib.tbl.FilterEvalBlock(f.h, b.h) })
	if err != nil {
		return FilterResult{}, err
	}
	return decodeFilterResult(raw)
}

// Block is a body parsed once. It holds its schema inside the library.
type Block struct {
	g   guard
	lib *Library
	h   *abi2.Block
}

func newBlock(l *Library, h *abi2.Block) *Block {
	b := &Block{lib: l, h: h}
	runtime.SetFinalizer(b, func(b *Block) { _ = b.Close() })
	return b
}

// Close releases the block, after the calls already inside it have returned.
// It is idempotent. A later call is a *UsageError.
func (b *Block) Close() error {
	if b == nil {
		return nil
	}
	b.g.shut(func() {
		runtime.SetFinalizer(b, nil)
		_ = b.h.Close()
	})
	return nil
}

func (b *Block) enter() error {
	if b == nil {
		return usageError("the block is nil")
	}
	return b.g.enter("the block")
}
