package chtypes

// errors.go — the error classes (bindings-v1.md section 4). Every call error
// carries the five fields of chs_error verbatim; the class of a status is
// spec/abi-v2/sdk.json's table, generated into the abi2 layer. A status the
// description does not list is its unknown(n) and an InternalError (rule r3).

import (
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

// CallError is the five fields every call error carries, verbatim and with
// nothing synthesized. It is embedded in *SchemaError, *UnsupportedError,
// *UsageError and *InternalError, so the fields read directly
// (se.ChCode); AsCallError reads them from any of the four.
type CallError struct {
	// Status is the raw chs_status value; compare with the Status constants.
	Status Status
	// ChCode is ClickHouse's own error code; nonzero only for a refusal.
	ChCode int32
	// ChName is the name this build's vendored table gives the code: ASCII,
	// empty if none.
	ChName string
	// Message is ClickHouse's own message for a refusal, the library's
	// otherwise. It is bytes, and may not be UTF-8; Error() is the one lossy
	// display form.
	Message string
	// Column is the column concerned, empty if none. It is bytes.
	Column string
}

// display is the one lossy display form: invalid UTF-8 replaced, for printing
// only.
func display(s string) string { return strings.ToValidUTF8(s, "�") }

func (e *CallError) text(class string) string {
	var b strings.Builder
	b.WriteString("chtypes: ")
	b.WriteString(class)
	b.WriteString(": ")
	b.WriteString(display(e.Message))
	if e.ChCode != 0 || e.ChName != "" {
		fmt.Fprintf(&b, " (ClickHouse code %d %s)", e.ChCode, display(e.ChName))
	}
	if e.Column != "" {
		fmt.Fprintf(&b, " (column %q)", display(e.Column))
	}
	return b.String()
}

// Error implements error for a bare CallError (the four classes below each
// override it with their own label).
func (e *CallError) Error() string { return e.text("call failed") }

// SchemaError is CHS_REJECTED: ClickHouse's own refusal, which a server would
// also give, including a zone name DateLUT will not load.
type SchemaError struct{ CallError }

func (e *SchemaError) Error() string { return e.text("schema error") }

// UnsupportedError is CHS_DECLINED: this build will not answer, where a server
// might accept. It is a PEER of SchemaError, never a subtype: a handler that
// forgot the distinction must not turn every decline into a rejection.
type UnsupportedError struct{ CallError }

func (e *UnsupportedError) Error() string { return e.text("unsupported") }

// UsageError is CHS_INVALID_ARGUMENT, and the misuse the binding detects before
// any call (a closed object, a refused version spelling, a conflicting setup,
// a zone given twice, an unverified open without both opt-ins). It carries the
// INVALID_ARGUMENT shape whichever side caught it.
type UsageError struct{ CallError }

func (e *UsageError) Error() string { return e.text("usage error") }

// InternalError is CHS_INTERNAL, a status outside the closed set, or a
// document that does not decode: a library bug.
type InternalError struct{ CallError }

func (e *InternalError) Error() string { return e.text("internal error") }

// AsCallError reads the five call-error fields from any of the four classes,
// however deeply err wraps one.
func AsCallError(err error) (*CallError, bool) {
	var se *SchemaError
	if errors.As(err, &se) {
		return &se.CallError, true
	}
	var ue *UnsupportedError
	if errors.As(err, &ue) {
		return &ue.CallError, true
	}
	var us *UsageError
	if errors.As(err, &us) {
		return &us.CallError, true
	}
	var ie *InternalError
	if errors.As(err, &ie) {
		return &ie.CallError, true
	}
	return nil, false
}

// usageError is the misuse the binding itself detects: INVALID_ARGUMENT shape,
// ch_code 0, an empty ch_name and column, and a message naming the misuse.
func usageError(format string, args ...any) error {
	return &UsageError{CallError{Status: StatusInvalidArgument, Message: fmt.Sprintf(format, args...)}}
}

// internalError is a document that does not decode (or any library bug the
// binding detects): bindings-v1.md section 8, question 10.
func internalError(format string, args ...any) error {
	return &InternalError{CallError{Status: StatusInternal, Message: fmt.Sprintf(format, args...)}}
}

// callError maps one abi2 call error by the D3 status table. A status outside
// the closed set is an InternalError naming its value.
func callError(c *abi2.CallError) error {
	if c == nil {
		return nil
	}
	ce := CallError{ChCode: c.ChCode, ChName: c.ChName, Message: c.Message, Column: c.Column}
	status, known := statusOfName(c.Status)
	if !known {
		raw := int64(StatusInternal)
		if n, err := strconv.ParseInt(strings.TrimPrefix(c.Status, "CHS_STATUS_"), 10, 32); err == nil {
			raw = n
		}
		// A status outside the closed set is its unknown(n) (rule r3), and the
		// call still fails: an internal error naming n.
		ce.Status = Status(raw)
		ce.Message = fmt.Sprintf("call status %s is outside the closed set: %s", ce.Status, c.Message)
		return &InternalError{ce}
	}
	ce.Status = status
	switch abi2.StatusClass(c.Status) {
	case "schema":
		return &SchemaError{ce}
	case "unsupported":
		return &UnsupportedError{ce}
	case "usage":
		return &UsageError{ce}
	default:
		return &InternalError{ce}
	}
}

// ErrorCode is one of the shared artifact codes, the fetch layer's own type.
type ErrorCode = ocifetch.ErrorCode

// The shared artifact codes (docs/guides/fetch-v1.md section 8), the fetch
// layer's own values; CodeArtifactIncompatible is the loader's.
const (
	CodeArtifactMissing      = ocifetch.CodeArtifactMissing
	CodeArtifactUntrusted    = ocifetch.CodeArtifactUntrusted
	CodeArtifactCorrupt      = ocifetch.CodeArtifactCorrupt
	CodeArtifactPinned       = ocifetch.CodeArtifactPinned
	CodeArtifactUnpublished  = ocifetch.CodeArtifactUnpublished
	CodeSourceUnreachable    = ocifetch.CodeSourceUnreachable
	CodeSourceUnauthorized   = ocifetch.CodeSourceUnauthorized
	CodeSourceForbidden      = ocifetch.CodeSourceForbidden
	CodeSourceIncompatible   = ocifetch.CodeSourceIncompatible
	CodeArtifactIncompatible = ocifetch.CodeArtifactIncompatible
	CodeCacheUnusable        = ocifetch.CodeCacheUnusable
)

// The sentinels, one per code: errors.Is(err, chtypes.ErrArtifactCorrupt) is
// true for the fetch layer's corrupt layer and the loader's step 5 alike. The
// nine fetch ones are the fetch layer's own values.
var (
	ErrArtifactMissing      = ocifetch.ErrArtifactMissing
	ErrArtifactUntrusted    = ocifetch.ErrArtifactUntrusted
	ErrArtifactCorrupt      = ocifetch.ErrArtifactCorrupt
	ErrArtifactPinned       = ocifetch.ErrArtifactPinned
	ErrArtifactUnpublished  = ocifetch.ErrArtifactUnpublished
	ErrSourceUnreachable    = ocifetch.ErrSourceUnreachable
	ErrSourceUnauthorized   = ocifetch.ErrSourceUnauthorized
	ErrSourceForbidden      = ocifetch.ErrSourceForbidden
	ErrSourceIncompatible   = ocifetch.ErrSourceIncompatible
	ErrArtifactIncompatible = errors.New(string(CodeArtifactIncompatible))
	ErrCacheUnusable        = ocifetch.ErrCacheUnusable
)

func sentinel(c ErrorCode) error {
	if c == CodeArtifactIncompatible {
		return ErrArtifactIncompatible
	}
	return c.Sentinel()
}

// ArtifactError is every fetch-time and load-time failure: one of the shared
// codes, what it concerns, and a complete message. errors.Is matches the
// sentinel for Code; errors.As reaches the struct. The fetch layer's errors
// and the loader's are one family: a caller catching ErrArtifactCorrupt
// catches both.
type ArtifactError struct {
	Code ErrorCode
	// Request is the version spelling as requested; Platform is
	// "<os>-<arch>"; Source is the base or path being read. Each is empty
	// when the failure has none.
	Request  string
	Platform string
	Source   string
	// Reason, Path, Want and Got describe a loader refusal: Reason is one of
	// sdk.json's loader.refusals reasons, with ":<symbol>" or ":<field>"
	// appended where it gives a suffix; Want and Got only where the refusal
	// names both. For CodeCacheUnusable, Path is the exact path that failed,
	// Reason one of unreadable_root, not_a_directory, unreadable_entry,
	// unacceptable_record, layout_0x and unwritable, and OSError the errno
	// name ("EACCES") when there is one.
	Reason  string
	Path    string
	Want    string
	Got     string
	OSError string
	// Msg is the complete message.
	Msg string
	// Err is the underlying cause, when there is one.
	Err error
}

func (e *ArtifactError) Error() string { return e.Msg }

// Unwrap exposes the underlying cause.
func (e *ArtifactError) Unwrap() error { return e.Err }

// Is makes errors.Is(err, ErrArtifact...) true for the matching code.
func (e *ArtifactError) Is(target error) bool { return target != nil && target == sentinel(e.Code) }

// fetchError wraps the fetch layer's error in this family. A fetch-layer error
// that is not a *ocifetch.FetchError (a refused spelling, a platform outside
// the set, a malformed option) is misuse.
func fetchError(err error) error {
	if err == nil {
		return nil
	}
	var fe *ocifetch.FetchError
	if errors.As(err, &fe) {
		return &ArtifactError{
			Code: fe.Code, Request: fe.Request, Platform: fe.Platform, Source: fe.Source,
			Path: fe.Path, Reason: fe.Reason, OSError: fe.OSError, Msg: fe.Msg, Err: fe,
		}
	}
	return usageError("%s", err.Error())
}

// loadError maps the abi2 loader's error: a refusal is an ArtifactError
// (incompatible or corrupt, by sdk.json's own table), a step 7 failure is the
// call's own error, and a refused unverified open is misuse.
func loadError(err error) error {
	var le *abi2.LoadError
	if errors.As(err, &le) {
		code := CodeArtifactIncompatible
		if le.Class() == "artifact_corrupt" {
			code = CodeArtifactCorrupt
		}
		var detail string
		switch {
		case le.Want != "":
			detail = fmt.Sprintf(" (want %q, got %q)", le.Want, le.Got)
		case le.Got != "":
			detail = fmt.Sprintf(" (%s)", le.Got)
		}
		msg := fmt.Sprintf("chtypes: %s refused: %s%s [%s]", le.Path, le.Reason, detail, code)
		if exact := le.Message(); exact != "" {
			// A dev SDK's fingerprint refusal carries rule r6's exact message.
			msg = exact
		}
		return &ArtifactError{
			Code: code, Reason: le.Reason, Path: le.Path, Want: le.Want, Got: le.Got, Err: le,
			Msg: msg,
		}
	}
	var ce *abi2.CallError
	if errors.As(err, &ce) {
		return callError(ce)
	}
	var ur *abi2.UnverifiedRefusedError
	if errors.As(err, &ur) {
		return usageError("opening %s without verification needs allow=true AND CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1", ur.Path)
	}
	return internalError("loading a library: %v", err)
}
