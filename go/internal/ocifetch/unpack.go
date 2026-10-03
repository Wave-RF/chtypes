package ocifetch

// unpack.go — bytes and unpack (docs/guides/fetch-v1.md §5): zstd-decode the
// verified layer (multi-frame streams accepted, the window log capped before
// the decoder allocates for it), then unpack the tar stream with per-entry
// checks — regular files and directories only — enforcing the unpacked-bytes
// cap as it is written, not after.

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/klauspost/compress/zstd"
)

// unpackResult is what unpackLibrary wrote into a fresh temp directory: the
// caller verifies librarySHA256/librarySize against the signed predicate,
// then renames dir into place (or discards it on a mismatch).
type unpackResult struct {
	dir           string
	libraryPath   string // relative to dir, exactly as the predicate named it
	librarySize   int64
	librarySHA256 string // hex, no "sha256:" prefix
}

// unpackLibrary decodes layerBytes (already verified against the manifest's
// layer descriptor by the caller) as a zstd-compressed tar stream into a
// fresh temp directory under parentDir, then locates libraryRelPath (the
// signed predicate's own "library" field) among the extracted regular files.
//
// Every check below runs in the order docs/guides/fetch-v1.md §5 states, and
// a failure at any step removes the temp directory it produced — no
// half-unpacked directory is ever left where a later --offline read could
// find it.
func unpackLibrary(layerBytes []byte, parentDir, libraryRelPath string) (result *unpackResult, err error) {
	tmp, err := os.MkdirTemp(parentDir, "unpack-*")
	if err != nil {
		return nil, fmt.Errorf("chtypes: creating a temp unpack directory: %w", err)
	}
	ok := false
	defer func() {
		if !ok {
			_ = os.RemoveAll(tmp)
		}
	}()

	zr, err := zstd.NewReader(bytes.NewReader(layerBytes),
		zstd.WithDecoderMaxWindow(1<<ZstdWindowLogMax),
		zstd.WithDecoderConcurrency(1))
	if err != nil {
		return nil, newError(CodeArtifactCorrupt, "", "", "", err, "zstd stream: %v", err)
	}
	defer zr.Close()

	tr := tar.NewReader(zr)
	seen := make(map[string]bool)
	var total int64
	wantName := normalizeTarName(libraryRelPath)
	var libSize int64
	var libSum [32]byte
	foundLib := false

	for {
		hdr, terr := tr.Next()
		if errors.Is(terr, io.EOF) {
			break
		}
		if terr != nil {
			return nil, newError(CodeArtifactCorrupt, "", "", "", terr, "tar stream: %v", terr)
		}
		name, verr := validateTarEntryName(hdr.Name)
		if verr != nil {
			return nil, newError(CodeArtifactCorrupt, "", "", "", verr, "tar entry %q: %v", hdr.Name, verr)
		}
		if seen[name] {
			return nil, newError(CodeArtifactCorrupt, "", "", "", nil, "tar stream has a duplicate entry %q", name)
		}
		seen[name] = true

		dest := filepath.Join(tmp, filepath.FromSlash(name))
		switch hdr.Typeflag {
		case tar.TypeDir:
			if merr := os.MkdirAll(dest, 0o755); merr != nil {
				return nil, merr
			}
		case tar.TypeReg:
			if merr := os.MkdirAll(filepath.Dir(dest), 0o755); merr != nil {
				return nil, merr
			}
			n, sum, werr := writeCapped(dest, tr, &total, MaxUnpackedBytes)
			if werr != nil {
				return nil, werr
			}
			if name == wantName {
				foundLib = true
				libSize = n
				libSum = sum
			}
		default:
			return nil, newError(CodeArtifactCorrupt, "", "", "", nil,
				"tar entry %q has disallowed type %v (only regular files and directories are allowed)", name, hdr.Typeflag)
		}
	}
	if !foundLib {
		return nil, newError(CodeArtifactCorrupt, "", "", "", nil, "tarball contains no file at %q (the signed predicate's library path)", libraryRelPath)
	}
	ok = true
	return &unpackResult{dir: tmp, libraryPath: wantName, librarySize: libSize, librarySHA256: hex.EncodeToString(libSum[:])}, nil
}

// validateTarEntryName refuses an absolute path or a ".." path segment,
// and normalizes a leading "./" (docs/guides/fetch-v1.md §5).
func validateTarEntryName(raw string) (string, error) {
	if raw == "" {
		return "", errors.New("empty entry name")
	}
	if strings.HasPrefix(raw, "/") {
		return "", errors.New("absolute path")
	}
	name := normalizeTarName(raw)
	for _, part := range strings.Split(name, "/") {
		if part == ".." {
			return "", errors.New(`contains a ".." path segment`)
		}
	}
	return name, nil
}

func normalizeTarName(name string) string {
	return strings.TrimPrefix(name, "./")
}

// writeCapped copies r into a new file at dest, enforcing maxUnpacked
// against *total (the running sum across the whole tar stream) as it reads,
// not after — a stream that would exceed the cap is refused mid-write, and
// the caller's defer removes everything written so far. Production always
// passes MaxUnpackedBytes; tests pass a smaller cap to exercise this path
// without writing gigabytes of data.
func writeCapped(dest string, r io.Reader, total *int64, maxUnpacked int64) (int64, [32]byte, error) {
	f, err := os.OpenFile(dest, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return 0, [32]byte{}, err
	}
	defer f.Close()

	h := sha256.New()
	var written int64
	buf := make([]byte, 32*1024)
	for {
		n, rerr := r.Read(buf)
		if n > 0 {
			*total += int64(n)
			if *total > maxUnpacked {
				return 0, [32]byte{}, newError(CodeArtifactCorrupt, "", "", "", nil,
					"unpacked content exceeds the %d byte cap", maxUnpacked)
			}
			if _, werr := f.Write(buf[:n]); werr != nil {
				return 0, [32]byte{}, werr
			}
			h.Write(buf[:n])
			written += int64(n)
		}
		if errors.Is(rerr, io.EOF) {
			break
		}
		if rerr != nil {
			return 0, [32]byte{}, rerr
		}
	}
	var sum [32]byte
	copy(sum[:], h.Sum(nil))
	return written, sum, nil
}
