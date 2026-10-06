package ocifetch

// lock.go — chtypes.lock, schema 3 (docs/guides/fetch-v1.md §6,
// spec/fetch-v1/schema/lock3.schema.json). A v0 lock (schema 1 or 2) is
// refused outright, never silently reinterpreted, as is a lock that fails
// schema 3 validation or names another ABI generation — all three name
// re-lock in their message. The index digest is informational only: it is
// never read back by --frozen (§7.4 of the layout-v2 spec: the index is
// computed by the host, not stored).

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// LockPin is one declared platform's resolved pin for one requested
// spelling.
type LockPin struct {
	Version  string `json:"version"`
	Build    string `json:"build"`
	Manifest Digest `json:"manifest"`
	Layer    Digest `json:"layer"`
	Bundle   Digest `json:"bundle"`
	Index    Digest `json:"index,omitempty"`
}

// Lock is chtypes.lock, schema 3.
type Lock struct {
	Schema    int                           `json:"schema"`
	ABI       int                           `json:"abi"`
	Platforms []string                      `json:"platforms"`
	Requests  map[string]map[string]LockPin `json:"requests"`
}

// readLock reads and validates a lock file, refusing anything that is not
// exactly schema 3 at this package's ABI generation.
func readLock(path string) (*Lock, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var probe struct {
		Schema int `json:"schema"`
	}
	if perr := strictUnmarshal(b, &probe); perr != nil {
		return nil, newError(CodeArtifactPinned, "", "", path, perr,
			"lock file %s is not valid JSON; re-lock with `fetch --lock`", path)
	}
	if probe.Schema != LockSchema {
		return nil, newError(CodeArtifactPinned, "", "", path, nil,
			"lock file %s is schema %d; v1 requires schema %d and never reinterprets an older one — re-lock with `fetch --lock`",
			path, probe.Schema, LockSchema)
	}
	var lock Lock
	if uerr := strictUnmarshal(b, &lock); uerr != nil {
		return nil, newError(CodeArtifactPinned, "", "", path, uerr,
			"lock file %s fails schema %d validation: %v; re-lock with `fetch --lock`", path, LockSchema, uerr)
	}
	if lock.ABI != active().abi {
		return nil, newError(CodeArtifactPinned, "", "", path, nil,
			"lock file %s names abi %d, this fetcher speaks abi %d; re-lock with `fetch --lock`", path, lock.ABI, active().abi)
	}
	return &lock, nil
}

// Pin looks up one (spelling, platform) pair. ok is false when the lock
// names no entry at all for it — the frozen-unpinned case, which the caller
// turns into CHTYPES_ARTIFACT_PINNED.
func (l *Lock) Pin(spelling, platform string) (LockPin, bool) {
	byPlatform, ok := l.Requests[spelling]
	if !ok {
		return LockPin{}, false
	}
	pin, ok := byPlatform[platform]
	return pin, ok
}

// writeLock writes lock to path by temp-then-rename.
func writeLock(path string, lock *Lock) error {
	sorted := *lock
	sorted.Platforms = append([]string(nil), lock.Platforms...)
	sortPlatformKeys(sorted.Platforms)
	out, err := json.MarshalIndent(&sorted, "", "  ")
	if err != nil {
		return err
	}
	out = append(out, '\n')
	return writeFileAtomic(filepath.Dir(path), path, out)
}

// mergeLockRequest returns a new Lock equal to existing (nil means "no
// existing lock") with spelling's pins replaced by pins, and platforms
// extended (never shrunk) to include every key pins declares. `fetch --lock`
// uses this: it touches only the spelling it just resolved, leaving every
// other spelling's pins untouched.
func mergeLockRequest(existing *Lock, spelling string, pins map[string]LockPin) *Lock {
	out := &Lock{Schema: LockSchema, ABI: active().abi, Requests: map[string]map[string]LockPin{}}
	if existing != nil {
		for k, v := range existing.Requests {
			out.Requests[k] = v
		}
		out.Platforms = append([]string(nil), existing.Platforms...)
	}
	out.Requests[spelling] = pins
	platformSet := map[string]bool{}
	for _, p := range out.Platforms {
		platformSet[p] = true
	}
	for p := range pins {
		platformSet[p] = true
	}
	out.Platforms = out.Platforms[:0]
	for p := range platformSet {
		out.Platforms = append(out.Platforms, p)
	}
	sortPlatformKeys(out.Platforms)
	return out
}

// rewriteLockForUpdate returns a new Lock carrying only freshResults: `update`
// re-resolves every locked request and rewrites the lock from scratch,
// "never merges a stale entry with a fresh one" (§6).
func rewriteLockForUpdate(freshResults map[string]map[string]LockPin) *Lock {
	out := &Lock{Schema: LockSchema, ABI: active().abi, Requests: map[string]map[string]LockPin{}}
	platformSet := map[string]bool{}
	for spelling, byPlatform := range freshResults {
		out.Requests[spelling] = byPlatform
		for p := range byPlatform {
			platformSet[p] = true
		}
	}
	for p := range platformSet {
		out.Platforms = append(out.Platforms, p)
	}
	sortPlatformKeys(out.Platforms)
	return out
}
