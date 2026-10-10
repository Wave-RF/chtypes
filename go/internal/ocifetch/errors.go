package ocifetch

// errors.go — the v1 shared error vocabulary (docs/guides/fetch-v1.md §8),
// twelve codes instead of v0's six. CHTYPES_ARTIFACT_INCOMPATIBLE is reserved
// for the FFI/loader layer (constants_gen.go's comment on ErrorExitCodes);
// this package's own code never constructs it.

import (
	"errors"
	"fmt"
	"net/url"
	"strings"
)

// ErrorCode is one of the v1 shared codes in constants_gen.go's
// ErrorExitCodes map. Its constants (CodeArtifactMissing …) are generated
// beside that map from spec/fetch-v1/constants.json. CodeArtifactIncompatible
// is the loader's: the fetch layer never raises it. CodeCacheUnusable's
// FetchError names the Path, the Reason and the OSError (§1, the cache
// faults; public issue #486). CodeSourceRetired is a source that answered 410
// Gone, never retried and never sent to the next base (§2, "A retired
// repository"; public issue #571).
type ErrorCode string

// Sentinel errors. errors.Is(err, ocifetch.ErrArtifactCorrupt) is true for
// any error this package produces with that code, however deeply wrapped.
var (
	ErrArtifactMissing     = errors.New(string(CodeArtifactMissing))
	ErrArtifactUntrusted   = errors.New(string(CodeArtifactUntrusted))
	ErrArtifactCorrupt     = errors.New(string(CodeArtifactCorrupt))
	ErrArtifactPinned      = errors.New(string(CodeArtifactPinned))
	ErrArtifactUnpublished = errors.New(string(CodeArtifactUnpublished))
	ErrSourceUnreachable   = errors.New(string(CodeSourceUnreachable))
	ErrSourceUnauthorized  = errors.New(string(CodeSourceUnauthorized))
	ErrSourceForbidden     = errors.New(string(CodeSourceForbidden))
	ErrSourceIncompatible  = errors.New(string(CodeSourceIncompatible))
	ErrCacheUnusable       = errors.New(string(CodeCacheUnusable))
	ErrSourceRetired       = errors.New(string(CodeSourceRetired))
)

// Sentinel returns the errors.Is target for this code, or nil for a code
// this package never constructs (CodeArtifactIncompatible).
func (c ErrorCode) Sentinel() error {
	switch c {
	case CodeArtifactMissing:
		return ErrArtifactMissing
	case CodeArtifactUntrusted:
		return ErrArtifactUntrusted
	case CodeArtifactCorrupt:
		return ErrArtifactCorrupt
	case CodeArtifactPinned:
		return ErrArtifactPinned
	case CodeArtifactUnpublished:
		return ErrArtifactUnpublished
	case CodeSourceUnreachable:
		return ErrSourceUnreachable
	case CodeSourceUnauthorized:
		return ErrSourceUnauthorized
	case CodeSourceForbidden:
		return ErrSourceForbidden
	case CodeSourceIncompatible:
		return ErrSourceIncompatible
	case CodeCacheUnusable:
		return ErrCacheUnusable
	case CodeSourceRetired:
		return ErrSourceRetired
	}
	return nil
}

// ExitCode looks the code up in the generated ErrorExitCodes table
// (constants_gen.go, sourced from spec/fetch-v1/constants.json). A code the
// table does not carry (there is none today) exits 1, the same floor v0 uses
// for every verification failure.
func (c ErrorCode) ExitCode() int {
	if code, ok := ErrorExitCodes[string(c)]; ok {
		return code
	}
	return 1
}

// FetchError is every error this package raises: one of the shared codes
// above, the request and platform it concerns, the source being read (empty
// for a cache-only lookup), and a complete message. errors.Is matches the
// sentinel for Code; errors.As reaches the struct for Code, Request, Platform
// and Source.
type FetchError struct {
	Code     ErrorCode
	Request  string // the spelling as requested: "26.8", "26.8.15.10", …
	Platform string // "<os>-<arch>"
	Source   string // the base URL or path being read; "" for a cache-only lookup
	// Path, Reason and OSError describe a CHTYPES_CACHE_UNUSABLE: the exact
	// path that failed, one of the Reason* values, and the errno name
	// ("EACCES") when there is one. They are empty for every other code.
	Path    string
	Reason  string
	OSError string
	Msg     string // the complete message, already prefixed "chtypes: "
	Err     error  // the underlying cause, when there is one
}

func (e *FetchError) Error() string { return e.Msg }

// Unwrap exposes the underlying cause for errors.As/errors.Unwrap.
func (e *FetchError) Unwrap() error { return e.Err }

// Is makes errors.Is(err, ocifetch.ErrArtifact…) true for the matching code.
func (e *FetchError) Is(target error) bool {
	return target != nil && e.Code.Sentinel() != nil && target == e.Code.Sentinel()
}

// ExitCode maps an error to its process exit status: 0 for nil, the code's
// own status for a *FetchError, and 1 for anything else.
func ExitCode(err error) int {
	if err == nil {
		return 0
	}
	var fe *FetchError
	if errors.As(err, &fe) {
		return fe.Code.ExitCode()
	}
	return 1
}

// newError builds a *FetchError. format/args produce the human-readable
// part of Msg; then every one of request, platform and source that is set and
// that text does not already name is added as a clause, "(request 26.8,
// platform linux-amd64, source https://…)", so the message alone carries what
// the error concerns (the public package's ArtifactError keeps only the
// message; public issue #500); the code is appended in brackets so a log
// line carries the shared vocabulary without the reader unwrapping anything.
func newError(code ErrorCode, request, platform, source string, cause error, format string, args ...any) *FetchError {
	text := fmt.Sprintf(format, args...)
	return &FetchError{
		Code:     code,
		Request:  request,
		Platform: platform,
		Source:   source,
		Err:      cause,
		Msg:      "chtypes: " + text + contextClause(text, request, platform, source) + " [" + string(code) + "]",
	}
}

// contextClause is newError's " (request …, platform …, source …)": each of
// the three that is set and that text does not already contain, or "" when
// there is none to add. A source URL's password, if it carries one, is
// redacted.
func contextClause(text, request, platform, source string) string {
	if u, err := url.Parse(source); err == nil && u.User != nil {
		source = u.Redacted()
	}
	var parts []string
	for _, p := range [...]struct{ name, value string }{
		{"request", request}, {"platform", platform}, {"source", source},
	} {
		if p.value != "" && !strings.Contains(text, p.value) {
			parts = append(parts, p.name+" "+p.value)
		}
	}
	if len(parts) == 0 {
		return ""
	}
	return " (" + strings.Join(parts, ", ") + ")"
}
