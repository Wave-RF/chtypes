package chtypes

// fetch_lock.go — pinning (docs/guides/fetch.md §5, schema 2 as of #284).
//
// `fetch --lock chtypes.lock` records, per <os>-<arch>/<clickhouse_version>
// (an EXACT patch, not a line, since #284), the asset file and sha256 that
// were installed, and the ABI revision that row carried; `fetch --frozen`
// refuses anything else with CHTYPES_ARTIFACT_PINNED — checking the revision
// FIRST, so a lock made for an ABI revision this binding no longer speaks is
// named as that, not as a drifted pin or an unpublished line. JSON:
//
//	{"schema": 2, "artifacts": {"linux-arm64/25.8.28.1-lts": {"file": "chtypes-25.8.28.1-lts-linux-arm64.tar.gz", "sha256": "…", "abi_revision": 6}}}
//
// Schema 1 (keyed by platform/LINE) is still read: each entry is converted
// to its schema-2 shape on the way in, from the exact version its own `file`
// names (docs/guides/artifacts.md's asset grammar), and the in-memory
// LockFile this package hands back is always schema-2-shaped — so every
// other file in this package only ever sees schema 2. The first WRITE of a
// schema-1 file therefore converts it in full; this package never writes
// schema 1.

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// LockSchema is the lock file schema this package writes, and the newest it
// reads.
const LockSchema = 2

// lockSchemaFloor is the oldest schema this package still reads (and
// converts on the way in).
const lockSchemaFloor = 1

// DefaultLockFile is the file `--frozen` reads when `--lock` names none.
const DefaultLockFile = "chtypes.lock"

// LockEntry pins one platform/patch to one release asset.
//
// ABIRevision is the ABI revision the pinned asset's row carried when this
// entry was written — optional and additive (docs/guides/fetch.md §5): an
// older SDK that has never heard of it writes and reads entries without it,
// and this one does not reject a lock that lacks it. A nil ABIRevision
// means either "this entry predates the field" or "the file was hand
// edited" — the two are indistinguishable, and both are treated the same
// way: the file/sha256 pin still enforces exactly as before.
type LockEntry struct {
	File   string `json:"file"`
	SHA256 string `json:"sha256"`
	// ABIRevision is omitted entirely when nil, never written as null, so a
	// reader that has never heard of the field sees exactly what it always
	// has.
	ABIRevision *int `json:"abi_revision,omitempty"`
}

// LockFile is the §5 document. In memory it is always schema-2-shaped: keys
// are "<os>-<arch>/<clickhouse_version>", never a minor line, whatever
// schema the file on disk declared.
type LockFile struct {
	Schema    int                  `json:"schema"`
	Artifacts map[string]LockEntry `json:"artifacts"`
}

// LockKey is the map key for one platform/patch pin: "<os>-<arch>/<exact>".
// The name is unchanged from schema 1 — only what the second half spells
// changed, from a minor line to an exact patch (#284).
func LockKey(platform, version string) string { return platform + "/" + version }

// NewLockFile returns an empty schema-2 lock file.
func NewLockFile() *LockFile {
	return &LockFile{Schema: LockSchema, Artifacts: map[string]LockEntry{}}
}

// assetFileName is docs/guides/artifacts.md's asset grammar:
// chtypes-<clickhouse_version>-<os>-<arch>[-b<build>].tar.gz. The version
// itself may carry a hyphen (a channel suffix, "25.8.28.1-lts"), so the
// platform is anchored at the END of the name rather than split naively.
var assetFileName = regexp.MustCompile(`^chtypes-(.+)-(linux|darwin)-(arm64|amd64)(?:-b[0-9]+)?\.tar\.gz$`)

// parseAssetFileName extracts the ClickHouse version and platform key from
// an asset file name, or reports ok=false when it does not parse.
func parseAssetFileName(file string) (version, platform string, ok bool) {
	m := assetFileName.FindStringSubmatch(file)
	if m == nil {
		return "", "", false
	}
	return m[1], m[2] + "-" + m[3], true
}

// convertSchema1 reads each "<os>-<arch>/<minor>" entry (schema 1) as the
// "<os>-<arch>/<exact>" entry (schema 2) it names: <exact> comes from the
// entry's own `file`, whose platform must match the key's and whose line
// must equal the key's minor (docs/guides/fetch.md §6). An entry that does
// not parse, or disagrees with its own key, makes the whole lock unreadable
// — the same as a malformed entry always has.
func convertSchema1(in map[string]LockEntry) (map[string]LockEntry, error) {
	out := make(map[string]LockEntry, len(in))
	for key, entry := range in {
		platform, minor, ok := strings.Cut(key, "/")
		if !ok {
			return nil, fmt.Errorf("%s: not a <os>-<arch>/<line> key", key)
		}
		version, filePlatform, ok := parseAssetFileName(entry.File)
		if !ok {
			return nil, fmt.Errorf("%s: file %q does not parse as a chtypes asset name", key, entry.File)
		}
		if filePlatform != platform {
			return nil, fmt.Errorf("%s: file %q names platform %s, not %s", key, entry.File, filePlatform, platform)
		}
		if minorOf(version) != minor {
			return nil, fmt.Errorf("%s: file %q is ClickHouse %s, whose line is %s, not %s", key, entry.File, version, minorOf(version), minor)
		}
		out[LockKey(platform, version)] = entry
	}
	return out, nil
}

// checkSchema2Keys refuses a schema-2 document whose own keys are not
// patch-shaped: a line-shaped key ("linux-arm64/25.8") reading as schema 2
// would silently pin nothing a patch request could ever match, which is a
// worse failure than refusing it up front, naming the key.
func checkSchema2Keys(artifacts map[string]LockEntry) error {
	for key := range artifacts {
		_, version, ok := strings.Cut(key, "/")
		if !ok {
			return fmt.Errorf("%s: not a <os>-<arch>/<version> key", key)
		}
		if _, exact, err := parseSpelling(version); err != nil || exact == "" {
			return fmt.Errorf("%s: a schema-%d lock key must name an exact patch, not a line", key, LockSchema)
		}
	}
	return nil
}

// ReadLockFile parses one. A missing file is reported through
// os.ErrNotExist (errors.Is), so a caller can decide whether that is fatal.
// Schema 1 and schema 2 are both accepted; any other number is refused,
// naming both. The document this returns is always schema-2-shaped.
func ReadLockFile(path string) (*LockFile, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var l LockFile
	if err := json.Unmarshal(b, &l); err != nil {
		return nil, fmt.Errorf("chtypes: %s: not a lock file: %w", path, err)
	}
	switch l.Schema {
	case LockSchema:
		if err := checkSchema2Keys(l.Artifacts); err != nil {
			return nil, fmt.Errorf("chtypes: %s: %w", path, err)
		}
	case lockSchemaFloor:
		converted, err := convertSchema1(l.Artifacts)
		if err != nil {
			return nil, fmt.Errorf("chtypes: %s: %w", path, err)
		}
		l.Artifacts = converted
		l.Schema = LockSchema
	default:
		return nil, fmt.Errorf("chtypes: %s: lock schema %d is not %d or %d — this SDK cannot read it",
			path, l.Schema, lockSchemaFloor, LockSchema)
	}
	if l.Artifacts == nil {
		l.Artifacts = map[string]LockEntry{}
	}
	return &l, nil
}

// readOrNewLockFile reads path, or starts a fresh document when it does not
// exist yet.
func readOrNewLockFile(path string) (*LockFile, error) {
	l, err := ReadLockFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return NewLockFile(), nil
	}
	return l, err
}

// Write stores the document atomically (temp sibling, rename) with keys in
// sorted order, so two fetches that pin the same things produce identical
// bytes and a diff on the file is a real change. Always written as schema
// 2 — the first write into a document read from a schema-1 file is what
// converts it (docs/guides/fetch.md §6).
func (l *LockFile) Write(path string) error {
	l.Schema = LockSchema
	b, err := json.MarshalIndent(l, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(b); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		os.Remove(tmpName)
		return err
	}
	return nil
}
