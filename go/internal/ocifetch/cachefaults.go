package ocifetch

// cachefaults.go — what can be wrong with a cache root, and what each mode
// does about it (docs/guides/fetch-v1.md §1, the cache faults; public issue
// #486). The default mode keeps "unreadable is absent" and says so in a
// warning; strict mode (StrictCache, CHTYPES_CACHE_STRICT=1, --strict) makes
// every fault a CHTYPES_CACHE_UNUSABLE naming the path, and never falls
// through to a system dir. A write the fetch layer needed that failed is
// CHTYPES_CACHE_UNUSABLE in every mode.

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// The reasons a CHTYPES_CACHE_UNUSABLE carries, the same in every binding.
const (
	ReasonUnreadableRoot     = "unreadable_root"     // K1: the root or unpacked/sha256/ cannot be listed
	ReasonNotADirectory      = "not_a_directory"     // K1: the root or unpacked/sha256/ is not a directory
	ReasonUnreadableEntry    = "unreadable_entry"    // K2: an entry or its verified.json cannot be read
	ReasonUnacceptableRecord = "unacceptable_record" // K3, strict only: a record no reader accepts, nothing to re-verify from
	ReasonLayout0x           = "layout_0x"           // K4, strict only: the cache is a 0.x registry directory
	ReasonUnwritable         = "unwritable"          // any mode: a write the fetch layer needed failed
)

// errnoNames are the errno spellings every binding reports, whatever the
// platform's numbers are.
var errnoNames = map[syscall.Errno]string{
	syscall.EACCES: "EACCES", syscall.EPERM: "EPERM", syscall.ENOENT: "ENOENT", syscall.ENOTDIR: "ENOTDIR",
	syscall.EISDIR: "EISDIR", syscall.EEXIST: "EEXIST", syscall.ENOTEMPTY: "ENOTEMPTY", syscall.EROFS: "EROFS",
	syscall.ENOSPC: "ENOSPC", syscall.EIO: "EIO", syscall.ELOOP: "ELOOP", syscall.ENAMETOOLONG: "ENAMETOOLONG",
	syscall.EXDEV: "EXDEV", syscall.EBUSY: "EBUSY", syscall.EMFILE: "EMFILE", syscall.ENFILE: "ENFILE",
	syscall.EDQUOT: "EDQUOT", syscall.EINVAL: "EINVAL",
}

// errnoName is the errno spelling of err's cause ("EACCES"), or "" when it
// carries none.
func errnoName(err error) string {
	var en syscall.Errno
	if !errors.As(err, &en) {
		return ""
	}
	if name, ok := errnoNames[en]; ok {
		return name
	}
	return fmt.Sprintf("errno %d", int(en))
}

// cacheFault is one thing wrong with a cache root.
type cacheFault struct {
	path, reason, osError string
}

// newCacheError builds the CHTYPES_CACHE_UNUSABLE for one fault: the same
// sentence in every binding, "<path> is unusable as a cache: <reason>
// (<errno>)".
func newCacheError(path, reason, osError string, cause error) *FetchError {
	detail := reason
	if osError != "" {
		detail += " (" + osError + ")"
	}
	if reason == ReasonLayout0x {
		if hint := zeroXHint(path); hint != "" {
			detail += ". " + hint
		}
	}
	return &FetchError{
		Code: CodeCacheUnusable, Source: path, Path: path, Reason: reason, OSError: osError, Err: cause,
		Msg: fmt.Sprintf("chtypes: %s is unusable as a cache: %s [%s]", path, detail, CodeCacheUnusable),
	}
}

func (f cacheFault) err() *FetchError { return newCacheError(f.path, f.reason, f.osError, nil) }

// warning is what the default mode says about a fault it treats as absent:
// the same sentence in every binding.
func (f cacheFault) warning() string {
	return fmt.Sprintf("%s could not be read (%s); treated as not installed. Set %s=1 to make this an error.",
		f.path, f.osError, EnvCacheStrictName)
}

// warns reports whether the default mode warns about f: an unusable root or
// an unreadable entry. An unacceptable record re-verifies or reads as absent,
// as §1 has it, and the 0.x shape is the MISSING answer's hint instead.
func (f cacheFault) warns() bool {
	return f.reason == ReasonUnreadableRoot || f.reason == ReasonNotADirectory || f.reason == ReasonUnreadableEntry
}

// probeRoot lists what is wrong with one root, in a fixed order: the root
// itself, then the 0.x shape (the cache only), then each entry by name. A
// root that does not exist is the empty cache, not a fault.
func probeRoot(root string, isCache bool) []cacheFault {
	fi, err := os.Stat(root)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return nil
	case err != nil:
		reason := ReasonUnreadableRoot
		if errors.Is(err, syscall.ENOTDIR) {
			reason = ReasonNotADirectory
		}
		return []cacheFault{{root, reason, errnoName(err)}}
	case !fi.IsDir():
		return []cacheFault{{root, ReasonNotADirectory, "ENOTDIR"}}
	}
	if isCache && zeroXShape(root) != "" {
		return []cacheFault{{root, ReasonLayout0x, ""}}
	}
	unpacked := filepath.Join(root, "unpacked")
	sha := filepath.Join(root, UnpackedDirName())
	entries, err := os.ReadDir(sha) // sorted by name
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return nil
	case errors.Is(err, syscall.ENOTDIR):
		path := sha
		if fi, serr := os.Stat(unpacked); serr == nil && !fi.IsDir() {
			path = unpacked
		}
		return []cacheFault{{path, ReasonNotADirectory, "ENOTDIR"}}
	case err != nil:
		// The path that blocks the listing: the first one that cannot be
		// passed through, else unpacked/sha256 itself.
		path := sha
		if _, serr := os.Stat(unpacked); errors.Is(serr, fs.ErrPermission) {
			path = root
		} else if _, serr := os.Stat(sha); errors.Is(serr, fs.ErrPermission) {
			path = unpacked
		}
		return []cacheFault{{path, ReasonUnreadableRoot, errnoName(err)}}
	}
	var faults []cacheFault
	for _, e := range entries {
		if !e.IsDir() || !recordHexPattern.MatchString(e.Name()) {
			continue
		}
		dir := filepath.Join(sha, e.Name())
		record := filepath.Join(dir, CacheVerifiedRecord)
		b, err := os.ReadFile(record)
		switch {
		case err == nil:
			if _, derr := decodeRecord(b); derr != nil && isCache && !blobExists(root, e.Name()) {
				faults = append(faults, cacheFault{record, ReasonUnacceptableRecord, ""})
			}
		case errors.Is(err, fs.ErrNotExist):
			// No record yet: a pre-seed or an unfinished install, which §1
			// treats as not installed.
		default:
			path := record
			if _, serr := os.Stat(record); errors.Is(serr, fs.ErrPermission) {
				path = dir
			}
			faults = append(faults, cacheFault{path, ReasonUnreadableEntry, errnoName(err)})
		}
	}
	return faults
}

// blobExists reports whether root holds the manifest blob an entry named hex
// could be re-verified from.
func blobExists(root, hex string) bool {
	_, err := os.Lstat(filepath.Join(root, "blobs", "sha256", hex))
	return err == nil
}

// probeRoots checks the cache, then every system dir, the way ro's mode asks.
// In strict mode the first fault is returned as its CHTYPES_CACHE_UNUSABLE,
// the cache's before any system dir's, so nothing falls through. In the
// default mode it returns one warning per unusable root or unreadable entry.
// A system dir that does not exist is skipped in both, as a default list.
func probeRoots(ro resolvedOptions) ([]string, error) {
	var warnings []string
	for i, dir := range append([]string{ro.cacheDir}, ro.systemDirs...) {
		for _, f := range probeRoot(dir, i == 0) {
			if ro.strict {
				if i > 0 && !f.warns() {
					continue // a system dir is never ours to re-verify, and has no 0.x shape to refuse
				}
				return nil, f.err()
			}
			if f.warns() {
				warnings = append(warnings, f.warning())
			}
		}
	}
	return warnings, nil
}

// ProbeCache checks the cache opts names, and its system dirs, by the mode
// opts asks for: the default mode's warnings, or strict mode's error (public
// issue #486). `chtypes where --strict` is this check.
func ProbeCache(opts *Options) ([]string, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	return probeRoots(ro)
}

// cacheIOError is a filesystem failure of a write the fetch layer needed,
// under the cache, as CHTYPES_CACHE_UNUSABLE with reason unwritable in every
// mode (public issue #486): never a UsageError, never the network's code. An
// error that is already a FetchError, or concerns a path outside the cache
// (the lock file, a file:// base), is returned as it came.
func cacheIOError(err error, cacheDir string) error {
	var fe *FetchError
	if err == nil || errors.As(err, &fe) {
		return err
	}
	var path string
	var pe *fs.PathError
	var le *os.LinkError
	switch {
	case errors.As(err, &pe):
		path = pe.Path
	case errors.As(err, &le):
		path = le.New
	default:
		return err
	}
	if rel, rerr := filepath.Rel(cacheDir, path); rerr != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return err
	}
	return newCacheError(path, ReasonUnwritable, errnoName(err), err)
}
