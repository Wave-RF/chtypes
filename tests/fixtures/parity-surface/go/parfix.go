// Package parfix is a fixture for scripts/parity-surface.py: the Go surface
// tests/fixtures/parity-surface/doc.md describes, plus one allowlisted extra
// (ExportNone). Nothing here does anything.
package parfix

import "errors"

// SetupOptions is the process setup.
type SetupOptions struct{ Timezone string }

// Setup records the setup.
func Setup(opts SetupOptions) error { _ = opts; return nil }

// Registry opens libraries.
type Registry struct{}

// NewRegistry constructs a registry.
func NewRegistry() (*Registry, error) { return &Registry{}, nil }

// For opens a version.
func (r *Registry) For(request string) (*Library, error) { _ = request; return &Library{}, nil }

// Installed lists what is installed.
func (r *Registry) Installed() ([]Resolved, error) { return nil, nil }

// Resolved is the fetch layer's record.
type Resolved struct {
	LibraryPath string
	Warnings    []string
}

// Library is one loaded image.
type Library struct{ Version string }

// OpenLinked opens the linked image.
func OpenLinked() (*Library, error) { return &Library{}, nil }

// ValidateType canonicalizes a type.
func (l *Library) ValidateType(typeExpr string) (string, error) { return typeExpr, nil }

// CompileTable compiles a table.
func (l *Library) CompileTable(createTable string, opts ...CompileOption) (*Schema, error) {
	_, _ = createTable, opts
	return &Schema{}, nil
}

// Schema is a compiled table.
type Schema struct{ closed bool }

// Close releases the schema.
func (s *Schema) Close() error { s.closed = true; return nil }

// CompileOption is an option of CompileTable.
type CompileOption interface{ compileOption() }

type settingsOption map[string]string

func (settingsOption) compileOption() {}

// WithSettings is the settings option.
func WithSettings(settings map[string]string) CompileOption { return settingsOption(settings) }

// Format is a format.
type Format int32

// The formats, and the export-off constant.
const (
	JSONEachRow Format = 0
	CSV         Format = 1
	ExportNone  Format = -1
)

// ChName is the format's ClickHouse name.
func (f Format) ChName() string { return [...]string{"JSONEachRow", "CSV"}[f] }

// Verdict is a filter verdict.
type Verdict string

// The verdicts.
const (
	VerdictTrue  Verdict = "t"
	VerdictFalse Verdict = "f"
)

// Answered says whether the verdict is an answer.
func (v Verdict) Answered() bool { return v == VerdictTrue || v == VerdictFalse }

// CallError is every call error's fields.
type CallError struct{ ChCode int32 }

func (e *CallError) Error() string { return "call error" }

// SchemaError is a refusal.
type SchemaError struct{ CallError }

func (e *SchemaError) Error() string { return "schema error" }

// AsCallError reads a call error's fields.
func AsCallError(err error) (*CallError, bool) {
	var se *SchemaError
	if errors.As(err, &se) {
		return &se.CallError, true
	}
	return nil, false
}

// ArtifactError is an artifact refusal.
type ArtifactError struct {
	Code   string
	Reason string
	Path   string
}

func (e *ArtifactError) Error() string { return e.Code }

// The sentinels.
var (
	ErrArtifactIncompatible = errors.New("CHTYPES_ARTIFACT_INCOMPATIBLE")
	ErrArtifactMissing      = errors.New("CHTYPES_ARTIFACT_MISSING")
	ErrArtifactPinned       = errors.New("CHTYPES_ARTIFACT_PINNED")
)

// BatchResult is a batch document.
type BatchResult struct {
	RowsRead    uint64
	PartitionID *string
	ColumnsSQL  string
	Spans       []Span
	Framing     *Framing
	Errors      []RowError
}

// Span is a byte range.
type Span struct{ Off, Len uint64 }

// Framing is the reader's framing.
type Framing struct {
	BomSkipped *bool
	Header     *Header
}

// Header is a header.
type Header struct {
	Consumed bool
	Lines    int
}

// RowError is one row's error.
type RowError struct {
	Row int
	Msg string
}

// ErrorCodeTable is the error-code table.
type ErrorCodeTable struct{ entries []ErrorCodeEntry }

// Name looks a code up.
func (t *ErrorCodeTable) Name(code int32) (string, bool) { _ = code; return "", false }

// All lists the table.
func (t *ErrorCodeTable) All() []ErrorCodeEntry { return t.entries }

// ErrorCodeEntry is one entry.
type ErrorCodeEntry struct {
	Code int32
	Name string
}
