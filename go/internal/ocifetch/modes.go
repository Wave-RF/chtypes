package ocifetch

// modes.go — what the cache's files and directories are created with
// (docs/guides/fetch-v1.md §1, "Modes"; public issue #486). Every file the
// fetch layer creates is opened with 0666 and every directory with 0777, and
// the process umask alone takes bits away: at the usual umask 022 a cache one
// uid writes is readable by every other uid, and readable is never writable.
// os.CreateTemp (0600) and os.MkdirTemp (0700) are never used for anything that
// is renamed into the cache, because the private mode would survive the rename
// and another uid would read the cache as empty.

import (
	"errors"
	"io/fs"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strconv"
)

const (
	// fileMode and dirMode are the modes a new file and a new directory are
	// asked for; the umask then applies, exactly as for any other program.
	fileMode fs.FileMode = 0o666
	dirMode  fs.FileMode = 0o777
)

// tempAttempts bounds the name retries of createTempFile and mkdirTemp. A
// collision needs another process to pick the same random name in the same
// directory, so a handful is plenty.
const tempAttempts = 64

func tempName(dir, prefix string) string {
	return filepath.Join(dir, prefix+strconv.FormatUint(rand.Uint64(), 36))
}

// createTempFile creates a new file named prefix plus a random suffix in dir,
// opened for writing, with mode 0666 less the umask.
func createTempFile(dir, prefix string) (*os.File, error) {
	var err error
	for range tempAttempts {
		var f *os.File
		f, err = os.OpenFile(tempName(dir, prefix), os.O_RDWR|os.O_CREATE|os.O_EXCL, fileMode)
		if err == nil {
			return f, nil
		}
		if !errors.Is(err, fs.ErrExist) {
			return nil, err
		}
	}
	return nil, err
}

// mkdirTemp creates a new directory named prefix plus a random suffix in dir,
// with mode 0777 less the umask.
func mkdirTemp(dir, prefix string) (string, error) {
	var err error
	for range tempAttempts {
		name := tempName(dir, prefix)
		if err = os.Mkdir(name, dirMode); err == nil {
			return name, nil
		}
		if !errors.Is(err, fs.ErrExist) {
			return "", err
		}
	}
	return "", err
}
