package main

import (
	"archive/tar"
	"bytes"

	"github.com/klauspost/compress/zstd"
)

// tarzstd.go — building the one-layer library tarball every platform
// manifest's layer blob is (layout-v2 spec §0: "one layer per platform
// manifest: the library tarball ... compressed with zstd -19"), plus the
// deliberately-bad tar entries and zstd shapes the "Bytes" conformance
// cases (docs/guides/fetch-v1.md §5) need.

const fakeLibraryName = "libchtypes.so" // darwin fixtures use libchtypes.dylib; see predicateFor

// fakeLibraryContent is deterministic filler, never a real binary: these
// fixtures only exercise the fetch layer (download, verify, unpack), which
// never dlopens or reads a symbol from what it installs (plan §1.3).
func fakeLibraryContent(seed string) []byte {
	return []byte("chtypes v1 conformance fixture library — not a real binary — seed:" + seed + "\n")
}

func zstdEncode(data []byte, opts ...zstd.EOption) []byte {
	var buf bytes.Buffer
	level := zstd.EncoderLevelFromZstd(19)
	allOpts := append([]zstd.EOption{zstd.WithEncoderLevel(level), zstd.WithEncoderConcurrency(1)}, opts...)
	enc, err := zstd.NewWriter(&buf, allOpts...)
	if err != nil {
		panic(err)
	}
	if _, err := enc.Write(data); err != nil {
		panic(err)
	}
	if err := enc.Close(); err != nil {
		panic(err)
	}
	return buf.Bytes()
}

// buildPlainTar writes one regular file (the library) plus one directory
// entry, in that order — directories are a valid, if unused by the
// library itself, tar entry type the unpack rule (§5: "regular files and
// directories only") must accept.
func buildPlainTar(libName string, libContent []byte) []byte {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	mustWriteHeader(tw, &tar.Header{Name: "docs/", Typeflag: tar.TypeDir, Mode: 0o755})
	mustWriteHeader(tw, &tar.Header{Name: libName, Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(libContent))})
	if _, err := tw.Write(libContent); err != nil {
		panic(err)
	}
	if err := tw.Close(); err != nil {
		panic(err)
	}
	return buf.Bytes()
}

type badTarKind int

const (
	badTarSymlink badTarKind = iota
	badTarHardlink
	badTarDevice
	badTarAbsPath
	badTarDotDot
	badTarDuplicateEntry
)

// buildBadTar writes one VALID library entry (so a binding that checked
// entry types too late would otherwise "succeed") plus one entry of the
// given bad kind. Every kind here must be refused as
// CHTYPES_ARTIFACT_CORRUPT (docs/guides/fetch-v1.md §5).
func buildBadTar(libName string, libContent []byte, kind badTarKind) []byte {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	mustWriteHeader(tw, &tar.Header{Name: libName, Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(libContent))})
	if _, err := tw.Write(libContent); err != nil {
		panic(err)
	}
	switch kind {
	case badTarSymlink:
		mustWriteHeader(tw, &tar.Header{Name: "evil-link", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd"})
	case badTarHardlink:
		mustWriteHeader(tw, &tar.Header{Name: "evil-hardlink", Typeflag: tar.TypeLink, Linkname: libName})
	case badTarDevice:
		mustWriteHeader(tw, &tar.Header{Name: "evil-device", Typeflag: tar.TypeChar, Devmajor: 1, Devminor: 3})
	case badTarAbsPath:
		mustWriteHeader(tw, &tar.Header{Name: "/etc/evil-absolute", Typeflag: tar.TypeReg, Mode: 0o644, Size: 4})
		if _, err := tw.Write([]byte("evil")); err != nil {
			panic(err)
		}
	case badTarDotDot:
		mustWriteHeader(tw, &tar.Header{Name: "../evil-dotdot", Typeflag: tar.TypeReg, Mode: 0o644, Size: 4})
		if _, err := tw.Write([]byte("evil")); err != nil {
			panic(err)
		}
	case badTarDuplicateEntry:
		// Same name as the library entry above, different content: an
		// implementation that keeps "the last entry wins" (tar's usual
		// convention for ordinary archives) would silently install
		// attacker-controlled bytes under a name the predicate already
		// vouched for.
		dup := []byte("duplicate entry, different bytes\n")
		mustWriteHeader(tw, &tar.Header{Name: libName, Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(dup))})
		if _, err := tw.Write(dup); err != nil {
			panic(err)
		}
	}
	if err := tw.Close(); err != nil {
		panic(err)
	}
	return buf.Bytes()
}

func mustWriteHeader(tw *tar.Writer, h *tar.Header) {
	if err := tw.WriteHeader(h); err != nil {
		panic(err)
	}
}

// buildMultiFrameZstd splits plaintext at its midpoint and encodes each
// half as its OWN zstd frame, concatenated — the standard zstd multi-frame
// convention (`cat a.zst b.zst` decodes to the concatenation of their
// plaintexts). zstd-multiframe must decode to byte-identical plaintext.
func buildMultiFrameZstd(plaintext []byte) []byte {
	mid := len(plaintext) / 2
	var out bytes.Buffer
	out.Write(zstdEncode(plaintext[:mid]))
	out.Write(zstdEncode(plaintext[mid:]))
	return out.Bytes()
}

// buildOversizeWindowZstd encodes plaintext with a window size above
// limits.zstd_window_log_max (27, i.e. 128 MiB) — zstd-window-too-large
// must be refused from the frame header alone, before the decoder commits
// to allocating a window that large.
func buildOversizeWindowZstd(plaintext []byte) []byte {
	return zstdEncode(plaintext, zstd.WithWindowSize(1<<28), zstd.WithSingleSegment(false))
}

// buildZipBombTarZstd streams totalSize bytes of a single repeated byte as
// one tar entry's content, which zstd compresses to a few hundred bytes —
// a decompression bomb small enough to commit to git, and exactly the
// shape limits.max_unpacked_bytes exists to refuse: the cap must be
// enforced AS the tar stream is written, not after a full decode that
// would otherwise try to allocate totalSize bytes.
func buildZipBombTarZstd(libName string, totalSize int64) []byte {
	// Stream the tar header, then totalSize bytes of filler, through the
	// zstd encoder directly — never materialize totalSize bytes as one Go
	// slice.
	var compressed bytes.Buffer
	enc, err := zstd.NewWriter(&compressed,
		zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(19)), zstd.WithEncoderConcurrency(1))
	if err != nil {
		panic(err)
	}
	tw := tar.NewWriter(enc)
	mustWriteHeader(tw, &tar.Header{Name: libName, Typeflag: tar.TypeReg, Mode: 0o644, Size: totalSize})
	chunk := bytes.Repeat([]byte{'A'}, 1<<20) // 1 MiB of filler, reused
	var written int64
	for written < totalSize {
		n := int64(len(chunk))
		if remaining := totalSize - written; remaining < n {
			n = remaining
		}
		if _, err := tw.Write(chunk[:n]); err != nil {
			panic(err)
		}
		written += n
	}
	if err := tw.Close(); err != nil {
		panic(err)
	}
	if err := enc.Close(); err != nil {
		panic(err)
	}
	return compressed.Bytes()
}
