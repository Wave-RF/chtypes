package chtypes

// fetch_lock.go — pinning (docs/fetch.md §5).
//
// `fetch --lock chtypes.lock` records, per <os>-<arch>/<minor>, the asset
// file and sha256 that were installed; `fetch --frozen` refuses anything
// else with CHTYPES_ARTIFACT_PINNED. Schema 1, JSON:
//
//	{"schema": 1, "artifacts": {"linux-arm64/25.8": {"file": "chtypes-25.8.28.1-lts-linux-arm64.tar.gz", "sha256": "…"}}}

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// LockSchema is the lock file schema this package reads and writes.
const LockSchema = 1

// DefaultLockFile is the file `--frozen` reads when `--lock` names none.
const DefaultLockFile = "chtypes.lock"

// LockEntry pins one platform/line to one release asset.
type LockEntry struct {
	File   string `json:"file"`
	SHA256 string `json:"sha256"`
}

// LockFile is the §5 document.
type LockFile struct {
	Schema    int                  `json:"schema"`
	Artifacts map[string]LockEntry `json:"artifacts"`
}

// LockKey is the map key for one platform/line: "<os>-<arch>/<minor>".
func LockKey(platform, minor string) string { return platform + "/" + minor }

// NewLockFile returns an empty schema-1 lock file.
func NewLockFile() *LockFile {
	return &LockFile{Schema: LockSchema, Artifacts: map[string]LockEntry{}}
}

// ReadLockFile parses one. A missing file is reported through
// os.ErrNotExist (errors.Is), so a caller can decide whether that is fatal.
func ReadLockFile(path string) (*LockFile, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var l LockFile
	if err := json.Unmarshal(b, &l); err != nil {
		return nil, fmt.Errorf("chtypes: %s: not a lock file: %w", path, err)
	}
	if l.Schema != LockSchema {
		return nil, fmt.Errorf("chtypes: %s: lock schema %d is not %d — this SDK cannot read it", path, l.Schema, LockSchema)
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
// bytes and a diff on the file is a real change.
func (l *LockFile) Write(path string) error {
	if l.Schema == 0 {
		l.Schema = LockSchema
	}
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
