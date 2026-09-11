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
	"strings"
	"syscall"
	"time"
)

// DefaultArtifactsURL is the public artifacts host.
const DefaultArtifactsURL = "https://artifacts.wavehouse.dev"

// DefaultReleaseTag is the rolling release a fetch reads when no tag is named.
const DefaultReleaseTag = "artifacts"

// errAssetNotFound: the source is reachable but has no file of that name.
var errAssetNotFound = errors.New("not found")

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
// the source answered and has no such file; any other error means the
// source could not be read.
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
	var last error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(time.Duration(attempt) * time.Second):
			}
		}
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
			// Retried on transient transport failures, as fetch.sh's
			// `curl --retry 3` is — which does not retry a refused
			// connection or a canceled context.
			if errors.Is(err, syscall.ECONNREFUSED) || ctx.Err() != nil {
				return nil, err
			}
			last = err
			continue
		}
		switch {
		case resp.StatusCode == http.StatusOK:
			return resp.Body, nil
		case resp.StatusCode == http.StatusNotFound:
			resp.Body.Close()
			return nil, fmt.Errorf("%s: HTTP 404: %w", url, errAssetNotFound)
		case resp.StatusCode >= 500:
			resp.Body.Close()
			last = fmt.Errorf("%s: HTTP %s", url, resp.Status)
			continue // a server failure is retried
		default:
			resp.Body.Close()
			return nil, fmt.Errorf("%s: HTTP %s", url, resp.Status)
		}
	}
	return nil, last
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
