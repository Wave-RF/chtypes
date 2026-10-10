//go:build unix

package ocifetch

import (
	"errors"
	"os"
	"syscall"
)

// lockShared takes flock(LOCK_SH) on f, waiting while an exclusive lock is
// held, and retrying a wait a signal interrupted.
func lockShared(f *os.File) error {
	for {
		err := syscall.Flock(int(f.Fd()), syscall.LOCK_SH)
		if !errors.Is(err, syscall.EINTR) {
			return err
		}
	}
}

// tryLockExclusive takes flock(LOCK_EX | LOCK_NB) on f, or fails at once.
func tryLockExclusive(f *os.File) error {
	return syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
}
