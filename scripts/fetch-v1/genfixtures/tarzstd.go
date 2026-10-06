package main

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"os"
	"strconv"

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
	b := []byte("chtypes v1 conformance fixture library — not a real binary — seed:" + seed + "\n")
	// PROBE ONLY (do not merge): one seed's library made large, so a
	// concurrent-install stress has a long unpack. A 1 MiB pseudo-random
	// block, repeated: about 1 MiB on the wire, PROBE_LARGE_LIBRARY_MIB on disk.
	if mib, _ := strconv.Atoi(os.Getenv("PROBE_LARGE_LIBRARY_MIB")); mib > 0 && seed == os.Getenv("PROBE_LARGE_LIBRARY_SEED") {
		block := make([]byte, 0, 1<<20)
		var ctr [8]byte
		for i := uint64(0); len(block) < 1<<20; i++ {
			binary.BigEndian.PutUint64(ctr[:], i)
			sum := sha256.Sum256(append([]byte(seed), ctr[:]...))
			block = append(block, sum[:]...)
		}
		for i := 0; i < mib; i++ {
			b = append(b, block...)
		}
	}
	return b
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

// zstdMagicNumber is the 4-byte little-endian frame magic every zstd frame
// starts with (RFC 8878 §3.1.1).
var zstdMagicNumber = []byte{0x28, 0xB5, 0x2F, 0xFD}

// buildOversizeWindowZstd encodes plaintext normally, then hand-patches the
// frame's own Window_Descriptor byte so it genuinely declares a windowLog
// above limits.zstd_window_log_max (27) — zstd-window-too-large must be
// refused from the frame header alone, before the decoder commits to
// allocating a window that large.
//
// zstd.WithWindowSize alone does not produce this: klauspost's encoder
// sizes the ACTUAL declared window to what the content needs (rounded up to
// the next power of two), not to the configured ceiling, so for this
// fixture's small plaintext the shipped frame declared a small window
// regardless of WithWindowSize(1<<28) — a correct client accepted it
// (measured: the Go and Python fetch lanes, independently, against
// cases.json on v1, 2026-10-02). Patching the header byte directly makes
// the DECLARATION itself oversized, independent of what the body needs.
func buildOversizeWindowZstd(plaintext []byte) []byte {
	frame := zstdEncode(plaintext, zstd.WithSingleSegment(false))
	patched := append([]byte(nil), frame...)
	patchZstdWindowDescriptor(patched, 25) // windowLog 35 (10+25) — far past the 27 cap
	mustDeclareOversizeWindow(patched)
	return patched
}

// patchZstdWindowDescriptor overwrites a frame's Window_Descriptor byte
// (frame offset 5, directly after the 4-byte magic number and the 1-byte
// Frame_Header_Descriptor) to declare windowLog = 10+exponent, mantissa 0.
// Per RFC 8878 §3.1.1, Window_Descriptor — when present at all — always
// immediately follows Frame_Header_Descriptor, before any Dictionary_ID or
// Frame_Content_Size field, so offset 5 does not depend on whether this
// frame carries either of those. Panics if the frame has no
// Window_Descriptor byte to patch (Single_Segment_flag is set).
func patchZstdWindowDescriptor(frame []byte, exponent byte) {
	if len(frame) < 6 || !bytes.Equal(frame[:4], zstdMagicNumber) {
		panic("genfixtures: patchZstdWindowDescriptor: not a zstd frame")
	}
	if frame[4]&0x20 != 0 { // Frame_Header_Descriptor bit 5: Single_Segment_flag
		panic("genfixtures: patchZstdWindowDescriptor: frame has Single_Segment_flag set, " +
			"so it has no Window_Descriptor byte at offset 5")
	}
	frame[5] = exponent << 3
}

// parseZstdWindowLog reads back the windowLog a frame's header declares.
// ok is false if the frame has no Window_Descriptor byte (Single_Segment_flag
// set) — the same shape patchZstdWindowDescriptor refuses to patch.
func parseZstdWindowLog(frame []byte) (windowLog int, ok bool) {
	if len(frame) < 6 || !bytes.Equal(frame[:4], zstdMagicNumber) {
		panic("genfixtures: parseZstdWindowLog: not a zstd frame")
	}
	if frame[4]&0x20 != 0 {
		return 0, false
	}
	exponent := frame[5] >> 3
	return 10 + int(exponent), true
}

// mustDeclareOversizeWindow is genfixtures' own self-check for the exact
// defect buildOversizeWindowZstd exists to fix: it parses the frame it is
// about to ship and panics unless the frame genuinely declares a windowLog
// above limits.zstd_window_log_max. Called every time this fixture is
// built (buildAll, --write and --check alike), so a regression here cannot
// silently ship again; selftestZstdWindowDeclaration (selftest.go) plants
// both a too-small window and a Single_Segment frame to prove this function
// actually panics on each.
func mustDeclareOversizeWindow(frame []byte) {
	gotLog, ok := parseZstdWindowLog(frame)
	if !ok {
		panic("genfixtures: zstd-window-too-large's frame has no Window_Descriptor byte " +
			"(Single_Segment_flag is set) — there is nothing to patch, and a Single_Segment frame's " +
			"window is defined to equal the content size, so it could never be oversized")
	}
	if gotLog <= C.Limits.ZstdWindowLogMax {
		panic(fmt.Sprintf("genfixtures: zstd-window-too-large's frame declares windowLog %d, which is "+
			"NOT above limits.zstd_window_log_max (%d) — the fixture would not actually exercise the "+
			"refusal it is named for", gotLog, C.Limits.ZstdWindowLogMax))
	}
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
