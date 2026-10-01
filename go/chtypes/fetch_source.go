package chtypes

// fetch_source.go — where artifacts come from (docs/guides/fetch.md §2).
//
// CHTYPES_ARTIFACTS_URL (default https://artifacts.wavehouse.dev) plus a
// release tag (default the rolling `artifacts`) gives <url>/<tag>/; an
// explicit base names anything else — an http(s) mirror, a file:// path,
// or a plain local directory. Every read is "<base>/<name>"; the caller
// walks the verification chain over the bytes.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// DefaultArtifactsURL is the public artifacts host.
const DefaultArtifactsURL = "https://artifacts.wavehouse.dev"

// DefaultReleaseTag is the rolling release a fetch reads when no tag is named.
const DefaultReleaseTag = "artifacts"

// errAssetNotFound: the source is reachable but has no file of that name.
// Raised for 404 and 410 alike — neither is ever retried (chtypes#365): a
// release either has a row or it does not, and that is decided at once.
var errAssetNotFound = errors.New("not found")

// retryableSourceError marks a source-level failure worth retrying through
// the existing docs/guides/fetch.md §3a budget (chtypes#365): an HTTP
// 5xx/408/429 response, or a connection-level failure reaching the host at
// all (refused, reset, timed out, DNS). retryAfter/hasRetryAfter carry the
// server's own requested wait, honored only for 503 and 429 — the two
// statuses this contract documents it for.
type retryableSourceError struct {
	err           error
	retryAfter    time.Duration
	hasRetryAfter bool
}

func (e *retryableSourceError) Error() string { return e.err.Error() }
func (e *retryableSourceError) Unwrap() error { return e.err }

// isRetryableStatus reports whether status is worth retrying: any 5xx, plus
// 408 (Request Timeout) and 429 (Too Many Requests) — chtypes#365. 404 and
// 410 are deliberately absent: those are errAssetNotFound, decided at once.
func isRetryableStatus(status int) bool {
	return (status >= 500 && status < 600) || status == http.StatusRequestTimeout || status == http.StatusTooManyRequests
}

// retryAfterFrom reads a Retry-After header, honored only on 503 and 429
// (docs/guides/fetch.md §3a, chtypes#365) — the pair this contract documents
// it for. It accepts both forms RFC 9110 §10.2.3 defines: delta-seconds and
// an HTTP-date. ok is false when the status does not carry one, the header
// is absent, or it does not parse as either form.
func retryAfterFrom(status int, header string, now time.Time) (d time.Duration, ok bool) {
	if status != http.StatusServiceUnavailable && status != http.StatusTooManyRequests {
		return 0, false
	}
	header = strings.TrimSpace(header)
	if header == "" {
		return 0, false
	}
	if secs, err := strconv.Atoi(header); err == nil {
		if secs < 0 {
			secs = 0
		}
		return time.Duration(secs) * time.Second, true
	}
	if t, err := http.ParseTime(header); err == nil {
		if d := t.Sub(now); d > 0 {
			return d, true
		}
		return 0, true
	}
	return 0, false
}

type source struct {
	remote bool   // true for http(s); false for a local directory
	base   string // URL without trailing slash, or a directory path
	client *http.Client
	token  string
}

// newSource resolves the §2 rules: an explicit base wins; otherwise the
// artifacts host and tag. A base that starts with http:// or https:// is
// remote; file:// is stripped; anything else is a directory as written.
func newSource(explicitURL, tag string, client *http.Client) (*source, error) {
	base := explicitURL
	if base == "" {
		host := os.Getenv(envArtifactsURL)
		if host == "" {
			host = DefaultArtifactsURL
		}
		if tag == "" {
			tag = DefaultReleaseTag
		}
		base = strings.TrimRight(host, "/") + "/" + tag
	} else if tag != "" {
		return nil, fmt.Errorf("chtypes: --url names a full base; --tag selects a release on the artifacts host — pass one")
	}
	s := &source{token: os.Getenv(envDownloadTok)}
	switch {
	case strings.HasPrefix(base, "http://"), strings.HasPrefix(base, "https://"):
		s.remote = true
		s.base = strings.TrimRight(base, "/")
		s.client = client
		if s.client == nil {
			s.client = &http.Client{}
		}
	case strings.HasPrefix(base, "file://"):
		s.base = strings.TrimPrefix(base, "file://")
	default:
		s.base = base
	}
	if !s.remote {
		s.base = filepath.Clean(s.base)
	}
	return s, nil
}

// String is the source as messages name it.
func (s *source) String() string { return s.base }

// open returns the bytes of one asset. errAssetNotFound (errors.Is) means
// the source answered and has no such file; a *retryableSourceError
// (errors.As) means the failure is worth retrying through the §3a budget
// (chtypes#365) — that retrying happens one layer up, around the whole
// consistent set or the whole tarball download, never here: a single
// request-level retry loop could not re-read anything but itself.
func (s *source) open(ctx context.Context, name string) (io.ReadCloser, error) {
	if !s.remote {
		f, err := os.Open(filepath.Join(s.base, name))
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				return nil, fmt.Errorf("%s: %w", name, errAssetNotFound)
			}
			return nil, err
		}
		return f, nil
	}
	url := s.base + "/" + name
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	if s.token != "" {
		req.Header.Set("Authorization", "Bearer "+s.token)
	}
	req.Header.Set("User-Agent", "chtypes-go-fetch")
	resp, err := s.client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return nil, err
		}
		// A connection-level failure reaching the host at all — refused,
		// reset, timed out, DNS — looks exactly like a blip, not a verdict
		// about the release (chtypes#365).
		return nil, &retryableSourceError{err: fmt.Errorf("%s: %w", url, err)}
	}
	switch {
	case resp.StatusCode == http.StatusOK:
		return resp.Body, nil
	case resp.StatusCode == http.StatusNotFound, resp.StatusCode == http.StatusGone:
		resp.Body.Close()
		return nil, fmt.Errorf("%s: HTTP %d: %w", url, resp.StatusCode, errAssetNotFound)
	case isRetryableStatus(resp.StatusCode):
		retryAfter, hasRetryAfter := retryAfterFrom(resp.StatusCode, resp.Header.Get("Retry-After"), time.Now())
		resp.Body.Close()
		return nil, &retryableSourceError{
			err:           fmt.Errorf("%s: HTTP %s", url, resp.Status),
			retryAfter:    retryAfter,
			hasRetryAfter: hasRetryAfter,
		}
	default:
		resp.Body.Close()
		return nil, fmt.Errorf("%s: HTTP %s", url, resp.Status)
	}
}

// readAll fetches one small asset whole (index.json, SHA256SUMS, the
// signature); the tarball streams through hashAndWrite instead.
func (s *source) readAll(ctx context.Context, name string, limit int64) ([]byte, error) {
	rc, err := s.open(ctx, name)
	if err != nil {
		return nil, err
	}
	defer rc.Close()
	b, err := io.ReadAll(io.LimitReader(rc, limit+1))
	if err != nil {
		return nil, fmt.Errorf("%s: %w", name, err)
	}
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("%s: larger than %d bytes — not a release asset of that kind", name, limit)
	}
	return b, nil
}
