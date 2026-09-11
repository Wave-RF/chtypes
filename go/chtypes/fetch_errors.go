package chtypes

// fetch_errors.go — the one error and the shared codes (docs/guides/fetch.md §7).
//
// Every SDK raises one identifiable error for a missing artifact, with one
// verbatim message, and reports every other fetch-time condition under one
// of six shared codes. In Go that is ErrArtifactMissing (a sentinel that
// works with errors.Is) and *ArtifactError, whose Code is the shared
// vocabulary and which matches the sentinel for its code through errors.Is.

import (
	"errors"
	"fmt"
	"strings"
)

// ErrorCode is the shared vocabulary of docs/guides/fetch.md §7 — the same six
// strings in Go, Python, TypeScript and Rust.
type ErrorCode string

const (
	// CodeArtifactMissing: no registry on the search path holds the line
	// (§1, §7). The loader's error; never a fetch outcome.
	CodeArtifactMissing ErrorCode = "CHTYPES_ARTIFACT_MISSING"
	// CodeArtifactUntrusted: SHA256SUMS.sig is absent, malformed, or does
	// not verify under any trusted key (§3 step 0, §4).
	CodeArtifactUntrusted ErrorCode = "CHTYPES_ARTIFACT_UNTRUSTED"
	// CodeArtifactCorrupt: any hash mismatch anywhere in the chain, or a
	// release that disagrees with itself (§3 steps 1–4).
	CodeArtifactCorrupt ErrorCode = "CHTYPES_ARTIFACT_CORRUPT"
	// CodeArtifactPinned: a lock file names a different asset or hash for
	// this platform/line, or does not name it at all, under --frozen (§5).
	CodeArtifactPinned ErrorCode = "CHTYPES_ARTIFACT_PINNED"
	// CodeArtifactUnpublished: the release has nothing for this
	// platform/line, or not the exact patch that was demanded (§2).
	CodeArtifactUnpublished ErrorCode = "CHTYPES_ARTIFACT_UNPUBLISHED"
	// CodeSourceUnreachable: the source cannot be read — no index.json,
	// a network failure, a listed asset that does not download — or the
	// fetch was told --offline and the line is not installed.
	CodeSourceUnreachable ErrorCode = "CHTYPES_SOURCE_UNREACHABLE"
)

// The sentinels. errors.Is(err, chtypes.ErrArtifactMissing) is true for any
// error this package produces with that code, however deeply wrapped; the
// same holds for each of the other five.
var (
	ErrArtifactMissing     = errors.New(string(CodeArtifactMissing))
	ErrArtifactUntrusted   = errors.New(string(CodeArtifactUntrusted))
	ErrArtifactCorrupt     = errors.New(string(CodeArtifactCorrupt))
	ErrArtifactPinned      = errors.New(string(CodeArtifactPinned))
	ErrArtifactUnpublished = errors.New(string(CodeArtifactUnpublished))
	ErrSourceUnreachable   = errors.New(string(CodeSourceUnreachable))
)

// Sentinel returns the errors.Is target for this code.
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
	}
	return nil
}

// ExitCode is the process exit status docs/guides/fetch.md §6 assigns to this code:
// 1 verification failed · 3 source unreachable · 4 not published.
func (c ErrorCode) ExitCode() int {
	switch c {
	case CodeSourceUnreachable:
		return 3
	case CodeArtifactUnpublished:
		return 4
	}
	return 1
}

// ArtifactError is every fetch-time or lookup-time failure this package
// reports: one of the six shared codes, the line and platform it concerns,
// the source it was reading (empty for a lookup), and a complete message.
// errors.Is matches the sentinel for Code; errors.As reaches the struct.
type ArtifactError struct {
	Code     ErrorCode
	Line     string // as requested: "25.8", or an exact patch
	Platform string // "<os>-<arch>"
	Source   string // the base URL or directory being read; "" for a lookup
	Msg      string // the complete message; for CodeArtifactMissing, verbatim per §7
	Err      error  // the underlying cause, when there is one
}

func (e *ArtifactError) Error() string { return e.Msg }

// Unwrap exposes the underlying cause (an *os.PathError, a *url.Error, …).
func (e *ArtifactError) Unwrap() error { return e.Err }

// Is makes errors.Is(err, ErrArtifact…) true for the matching code.
func (e *ArtifactError) Is(target error) bool { return target != nil && target == e.Code.Sentinel() }

// ExitCode maps an error to the docs/guides/fetch.md §6 exit status: 0 for nil,
// the code's own status for an *ArtifactError, and 1 for anything else.
// Usage errors are the CLI's business (2) and never come out of this
// package as *ArtifactError.
func ExitCode(err error) int {
	if err == nil {
		return 0
	}
	var ae *ArtifactError
	if errors.As(err, &ae) {
		return ae.Code.ExitCode()
	}
	return 1
}

// GoFetchCommand is this SDK's fetch command, as the §7 message spells it.
const GoFetchCommand = "go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch"

// missingArtifactError builds the §7 error, message verbatim apart from the
// bracketed parts: the line as requested, the platform, and every directory
// that was looked in, in search order.
func missingArtifactError(line, platform string, looked []string) *ArtifactError {
	return &ArtifactError{
		Code:     CodeArtifactMissing,
		Line:     line,
		Platform: platform,
		Msg: fmt.Sprintf("chtypes: no artifact for ClickHouse %s (%s). Looked in: %s.\n"+
			"Install it:  %s %s\n"+
			"or set CHTYPES_AUTOFETCH=1 to fetch on first use.",
			line, platform, strings.Join(looked, ", "), GoFetchCommand, line),
	}
}

// artifactErrorf builds an *ArtifactError for the fetch path: the message
// is prefixed "chtypes: " and suffixed with the code in brackets so a log
// line carries the shared vocabulary without the reader unwrapping anything.
func artifactErrorf(code ErrorCode, line, platform, source string, cause error, format string, args ...any) *ArtifactError {
	return &ArtifactError{
		Code:     code,
		Line:     line,
		Platform: platform,
		Source:   source,
		Err:      cause,
		Msg:      "chtypes: " + fmt.Sprintf(format, args...) + " [" + string(code) + "]",
	}
}
