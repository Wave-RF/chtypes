package ocifetch

// tags.go — the two reads the CLI needs that are not part of the seam:
// the published lines (the registry's tags/list, filtered to two-part
// spellings, docs/guides/fetch-v1.md §3) and the resolved cache root
// (§1). Neither verifies anything: a tag listing is discovery, and every
// line it names is still verified in full when a caller fetches it.

import (
	"context"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

var lineTag = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)

// ListTags returns the floating two-part tags (the published lines) the
// first base that answers lists, sorted by version. A base that answers with
// something that is not a tag list is CHTYPES_SOURCE_INCOMPATIBLE.
func ListTags(ctx context.Context, opts *Options) ([]string, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	s := newSession(ro)
	result, _, err := s.fetchAcrossBases(ctx, ro.bases, "tags/list", notFoundUnpublished,
		requestOptions{maxBytes: TagsListMaxBytes})
	if err != nil {
		return nil, err
	}
	var doc struct {
		Tags []string `json:"tags"`
	}
	if uerr := strictUnmarshal(result.body, &doc); uerr != nil {
		return nil, newError(CodeSourceIncompatible, "", "", result.url, uerr, "%s is not a tag list: %v", result.url, uerr)
	}
	var lines []string
	for _, t := range doc.Tags {
		if lineTag.MatchString(t) {
			lines = append(lines, t)
		}
	}
	sort.Slice(lines, func(i, j int) bool { return lineLess(lines[i], lines[j]) })
	return lines, nil
}

func lineLess(a, b string) bool {
	ap, bp := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < 2; i++ {
		x, _ := strconv.Atoi(ap[i])
		y, _ := strconv.Atoi(bp[i])
		if x != y {
			return x < y
		}
	}
	return false
}

// CacheRoot is the layout directory a call with opts would use: CacheDir,
// else CHTYPES_CACHE, each through the dev channel's v2-dev subroot (rule r5),
// else the per-user default (§1).
func CacheRoot(opts *Options) (string, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return "", err
	}
	return ro.cacheDir, nil
}

// SearchDirs is every directory a lookup with opts reads, in the order it
// reads them: the cache root (CacheRoot), then each system directory. It
// creates and reads nothing.
func SearchDirs(opts *Options) ([]string, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	return ro.searchDirs(), nil
}

// ReadLock reads and validates a lock file (§6): schema 3 at this ABI
// generation, or CHTYPES_ARTIFACT_PINNED naming re-lock.
func ReadLock(path string) (*Lock, error) { return readLock(path) }
