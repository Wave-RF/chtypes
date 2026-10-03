"""zstd decode + safe tar extraction (`_unpack.py`)."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

try:
    from compression import zstd
except ImportError:
    from backports import zstd  # type: ignore[no-redef]

from chtypes._ocifetch._errors import ArtifactCorruptError
from chtypes._ocifetch._unpack import unpack_tar_zst

LIBRARY_NAME = "libchtypes.so"
LIBRARY_BYTES = b"fake native library bytes, deterministic for tests"


def _make_tar(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for info, data in members:
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return buf.getvalue()


def _compress(data: bytes, *, window_log: int | None = None, long_distance: bool = False) -> bytes:
    if window_log is None:
        return zstd.compress(data)
    opts = {zstd.CompressionParameter.window_log: window_log}
    if long_distance:
        opts[zstd.CompressionParameter.enable_long_distance_matching] = 1
    compressor = zstd.ZstdCompressor(options=opts)
    return compressor.compress(data) + compressor.flush()


def _write(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    return str(path)


def _basic_tar_bytes(extra: list[tuple[tarfile.TarInfo, bytes | None]] | None = None) -> bytes:
    info = tarfile.TarInfo(name=LIBRARY_NAME)
    members = [(info, LIBRARY_BYTES)]
    if extra:
        members.extend(extra)
    return _make_tar(members)


def _unpack(tmp_path: Path, tar_bytes: bytes, **kwargs) -> Path:
    compressed_path = _write(tmp_path / "layer.tar.zst", _compress(tar_bytes))
    dest = tmp_path / "dest"
    unpack_tar_zst(
        compressed_path,
        str(dest),
        library_name=kwargs.pop("library_name", LIBRARY_NAME),
        library_sha256=kwargs.pop("library_sha256", hashlib.sha256(LIBRARY_BYTES).hexdigest()),
        library_bytes=kwargs.pop("library_bytes", len(LIBRARY_BYTES)),
        **kwargs,
    )
    return dest


def test_unpack_happy_path(tmp_path: Path) -> None:
    dest = _unpack(tmp_path, _basic_tar_bytes())
    assert (dest / LIBRARY_NAME).read_bytes() == LIBRARY_BYTES


def test_unpack_with_directories(tmp_path: Path) -> None:
    dir_info = tarfile.TarInfo(name="sub")
    dir_info.type = tarfile.DIRTYPE
    nested = tarfile.TarInfo(name="sub/extra.txt")
    dest = _unpack(tmp_path, _basic_tar_bytes([(dir_info, None), (nested, b"x")]))
    assert (dest / "sub" / "extra.txt").read_bytes() == b"x"


def test_unpack_rejects_symlink(tmp_path: Path) -> None:
    link = tarfile.TarInfo(name="evil-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(ArtifactCorruptError, match="link"):
        _unpack(tmp_path, _basic_tar_bytes([(link, None)]))


def test_unpack_rejects_hardlink(tmp_path: Path) -> None:
    link = tarfile.TarInfo(name="evil-hardlink")
    link.type = tarfile.LNKTYPE
    link.linkname = LIBRARY_NAME
    with pytest.raises(ArtifactCorruptError, match="link"):
        _unpack(tmp_path, _basic_tar_bytes([(link, None)]))


def test_unpack_rejects_device(tmp_path: Path) -> None:
    dev = tarfile.TarInfo(name="evil-device")
    dev.type = tarfile.CHRTYPE
    with pytest.raises(ArtifactCorruptError, match="device"):
        _unpack(tmp_path, _basic_tar_bytes([(dev, None)]))


def test_unpack_rejects_absolute_path(tmp_path: Path) -> None:
    info = tarfile.TarInfo(name="/etc/evil")
    with pytest.raises(ArtifactCorruptError, match="unsafe"):
        _unpack(tmp_path, _basic_tar_bytes([(info, b"x")]))


def test_unpack_rejects_dotdot_traversal(tmp_path: Path) -> None:
    info = tarfile.TarInfo(name="../../evil")
    with pytest.raises(ArtifactCorruptError, match="unsafe"):
        _unpack(tmp_path, _basic_tar_bytes([(info, b"x")]))


def test_unpack_rejects_duplicate_entry(tmp_path: Path) -> None:
    dup = tarfile.TarInfo(name=LIBRARY_NAME)
    with pytest.raises(ArtifactCorruptError, match="duplicate"):
        _unpack(tmp_path, _basic_tar_bytes([(dup, b"second copy")]))


def test_unpack_rejects_over_cap(tmp_path: Path) -> None:
    with pytest.raises(ArtifactCorruptError, match="cap"):
        _unpack(tmp_path, _basic_tar_bytes(), max_unpacked_bytes=len(LIBRARY_BYTES) - 1)


def test_unpack_rejects_oversize_zstd_window(tmp_path: Path) -> None:
    tar_bytes = _basic_tar_bytes()
    compressed = _compress(tar_bytes, window_log=28, long_distance=True)
    compressed_path = _write(tmp_path / "layer.tar.zst", compressed)
    with pytest.raises(ArtifactCorruptError, match="zstd"):
        unpack_tar_zst(
            compressed_path,
            str(tmp_path / "dest"),
            library_name=LIBRARY_NAME,
            library_sha256=hashlib.sha256(LIBRARY_BYTES).hexdigest(),
            library_bytes=len(LIBRARY_BYTES),
            window_log_max=27,
        )


def test_unpack_accepts_multiframe_zstd(tmp_path: Path) -> None:
    tar_bytes = _basic_tar_bytes()
    half = len(tar_bytes) // 2
    multi = zstd.compress(tar_bytes[:half]) + zstd.compress(tar_bytes[half:])
    compressed_path = _write(tmp_path / "layer.tar.zst", multi)
    dest = tmp_path / "dest"
    unpack_tar_zst(
        compressed_path,
        str(dest),
        library_name=LIBRARY_NAME,
        library_sha256=hashlib.sha256(LIBRARY_BYTES).hexdigest(),
        library_bytes=len(LIBRARY_BYTES),
    )
    assert (dest / LIBRARY_NAME).read_bytes() == LIBRARY_BYTES


def test_unpack_rejects_library_hash_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ArtifactCorruptError, match="sha256"):
        _unpack(tmp_path, _basic_tar_bytes(), library_sha256="0" * 64)


def test_unpack_rejects_library_size_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ArtifactCorruptError, match="size"):
        _unpack(tmp_path, _basic_tar_bytes(), library_bytes=len(LIBRARY_BYTES) + 1)


def test_unpack_rejects_missing_library_entry(tmp_path: Path) -> None:
    info = tarfile.TarInfo(name="not-the-library")
    with pytest.raises(ArtifactCorruptError, match="no entry named"):
        _unpack(tmp_path, _make_tar([(info, b"x")]))
