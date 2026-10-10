//go:build !unix

package ocifetch

import (
	"errors"
	"os"
)

// Every platform this module serves is unix; elsewhere nothing can be locked,
// so nothing is held and prune keeps every build.

func lockShared(*os.File) error { return errors.ErrUnsupported }

func tryLockExclusive(*os.File) error { return errors.ErrUnsupported }
