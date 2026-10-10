package ocifetch

// hold.go — the in-use signal (docs/guides/fetch-v1.md §1, "In use"; public
// issue #494). A process that is about to load an installed build, or that a
// registry's fetch handed one to, takes a SHARED advisory lock (flock) on the
// entry's verified.json and keeps it until the process exits. `chtypes prune`
// removes an entry only after it has taken the EXCLUSIVE lock without waiting,
// so it never removes a build a live process holds. The kernel drops a dead
// process's locks, so a crash never pins a build. flock belongs to the open
// file description on Linux and darwin, so a hold taken by this very process
// counts too, and so does one taken by any binding: all four lock the same
// file the same way.

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"sync"
	"syscall"
)

// HoldState is what Hold found.
type HoldState int

const (
	// HoldHeld: this process holds the entry, shared, for its life.
	HoldHeld HoldState = iota
	// HoldUnheld: the record cannot be locked here (a filesystem without
	// flock, or an error other than the entry being gone). The caller goes on:
	// a prune on the same filesystem cannot take its exclusive lock either, so
	// it keeps the entry.
	HoldUnheld
	// HoldVanished: the entry is gone, or another entry has replaced it: a
	// prune removed it after the lookup chose it. The caller looks again.
	HoldVanished
)

// holds are this process's shared locks, one open record per entry
// directory, kept until the process exits (a held *os.File is reachable, so
// its finalizer never closes it).
var holds struct {
	sync.Mutex
	files map[string]*os.File
}

// Hold takes this process's shared hold on the installed entry dir
// (<root>/unpacked/sha256/<manifest hex>) and keeps it for the life of the
// process. It waits only while a prune holds the entry exclusively, which a
// prune does across one rename; then the entry is gone, and Hold says so.
func Hold(dir string) HoldState {
	record := filepath.Join(dir, CacheVerifiedRecord)
	holds.Lock()
	defer holds.Unlock()
	if f, ok := holds.files[dir]; ok {
		if sameFile(f, record) {
			return HoldHeld
		}
		// The entry this process held was removed and installed again: the
		// old hold protects nothing now.
		_ = f.Close()
		delete(holds.files, dir)
	}
	f, err := os.Open(record)
	if err != nil {
		if gone(err) {
			return HoldVanished
		}
		return HoldUnheld
	}
	if err := lockShared(f); err != nil {
		_ = f.Close()
		return HoldUnheld
	}
	if !sameFile(f, record) {
		_ = f.Close()
		return HoldVanished
	}
	if holds.files == nil {
		holds.files = map[string]*os.File{}
	}
	holds.files[dir] = f
	return HoldHeld
}

// claimState is what claim found.
type claimState int

const (
	claimOwned claimState = iota // locked exclusively: no process holds it
	claimInUse                   // a process holds it, or it cannot be locked: kept
	claimGone                    // gone or replaced since it was listed: not ours to report
)

// claim takes the exclusive lock prune needs on the entry dir, without
// waiting. It is owned only when no process holds the entry and the record
// it locked is still the one at the path; release drops the lock.
func claim(dir string) (claimState, func()) {
	record := filepath.Join(dir, CacheVerifiedRecord)
	f, err := os.Open(record)
	if err != nil {
		if gone(err) {
			return claimGone, nil
		}
		return claimInUse, nil
	}
	if err := tryLockExclusive(f); err != nil {
		_ = f.Close()
		return claimInUse, nil
	}
	if !sameFile(f, record) {
		_ = f.Close()
		return claimGone, nil
	}
	return claimOwned, func() { _ = f.Close() }
}

// sameFile reports whether the open file f is still the file at path.
func sameFile(f *os.File, path string) bool {
	held, err := f.Stat()
	if err != nil {
		return false
	}
	now, err := os.Stat(path)
	if err != nil {
		return false
	}
	return os.SameFile(held, now)
}

func gone(err error) bool {
	return errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ENOTDIR)
}

// RemovedWhileHeld is CHTYPES_ARTIFACT_MISSING for a request whose build a
// concurrent prune removed twice, each time between its install and this
// process's hold.
func RemovedWhileHeld(request, dir string) *FetchError {
	return newError(CodeArtifactMissing, request, "", "", nil,
		"%s was removed by a concurrent prune before this process could hold it; ask for %s again", dir, request)
}
