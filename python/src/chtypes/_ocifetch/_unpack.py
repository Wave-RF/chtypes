"""Bytes: zstd decode, then the tar rules, then the library re-hash.

layout-v2 spec §7.3 / PLAN "Lane Python": verify size and sha256 BEFORE any
decompression (done by the caller, `_ensure.py`, on the still-compressed
temp file); only then zstd-decode and unpack, entries restricted to regular
files and directories, with a byte cap, and a final re-hash of the named
library file against the signed predicate.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
from pathlib import PurePosixPath

try:
    from compression import zstd  # Python 3.14+ (PEP 784)
except ImportError:  # Python 3.11-3.13
    from backports import zstd  # type: ignore[no-redef]

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._errors import ArtifactCorruptError

__all__ = ["unpack_tar_zst"]


def _is_safe_member_name(name: str) -> bool:
    if name in ("", "."):
        return False
    posix = PurePosixPath(name)
    if posix.is_absolute():
        return False
    if any(part == ".." for part in posix.parts):
        return False
    return True


def _reject_unsafe(member: tarfile.TarInfo) -> None:
    if not _is_safe_member_name(member.name):
        raise ArtifactCorruptError(f"unsafe tar member path {member.name!r}")
    if member.islnk() or member.issym():
        raise ArtifactCorruptError(f"tar member {member.name!r} is a link, refused")
    if member.ischr() or member.isblk() or member.isfifo():
        raise ArtifactCorruptError(f"tar member {member.name!r} is a device/fifo, refused")
    if not (member.isreg() or member.isdir()):
        raise ArtifactCorruptError(
            f"tar member {member.name!r} is neither a regular file nor a directory"
        )


def unpack_tar_zst(
    compressed_path: str,
    dest_dir: str,
    *,
    library_name: str,
    library_sha256: str,
    library_bytes: int,
    max_unpacked_bytes: int = C.MAX_UNPACKED_BYTES,
    window_log_max: int = C.ZSTD_WINDOW_LOG_MAX,
) -> None:
    """Decompress and extract ``compressed_path`` (already hash/size-verified
    by the caller) into ``dest_dir`` (a fresh, caller-owned temp directory —
    this never renames anything into the cache itself; `_layout.py` does
    that once this returns successfully).

    Raises `ArtifactCorruptError` for: a zstd window above
    ``window_log_max``, any tar member that is not a plain file or
    directory, an absolute or `..`-escaping path, a duplicate entry, total
    extracted bytes over ``max_unpacked_bytes``, or a library whose own
    content disagrees with ``library_sha256``/``library_bytes``.
    """
    os.makedirs(dest_dir, exist_ok=True)
    seen_names: set[str] = set()
    total_bytes = 0
    library_hash = hashlib.sha256()
    library_size = 0
    found_library = False

    options = {zstd.DecompressionParameter.window_log_max: window_log_max}
    try:
        with open(compressed_path, "rb") as raw:
            with zstd.ZstdFile(raw, mode="rb", options=options) as decompressed:
                with tarfile.open(fileobj=decompressed, mode="r|") as tar:
                    for member in tar:
                        _reject_unsafe(member)
                        if member.name in seen_names:
                            raise ArtifactCorruptError(f"duplicate tar entry {member.name!r}")
                        seen_names.add(member.name)
                        dest_path = os.path.join(dest_dir, member.name)
                        if member.isdir():
                            os.makedirs(dest_path, exist_ok=True)
                            continue
                        os.makedirs(os.path.dirname(dest_path) or dest_dir, exist_ok=True)
                        total_bytes += member.size
                        if total_bytes > max_unpacked_bytes:
                            raise ArtifactCorruptError(
                                f"unpacked size exceeded the {max_unpacked_bytes}-byte cap"
                            )
                        src = tar.extractfile(member)
                        if src is None:
                            raise ArtifactCorruptError(f"tar member {member.name!r} had no data")
                        is_library = member.name == library_name
                        hasher = hashlib.sha256() if is_library else None
                        with open(dest_path, "wb") as out:
                            while True:
                                chunk = src.read(1024 * 1024)
                                if not chunk:
                                    break
                                out.write(chunk)
                                if hasher is not None:
                                    hasher.update(chunk)
                        if is_library:
                            found_library = True
                            library_hash = hasher
                            library_size = member.size
    except zstd.ZstdError as e:
        raise ArtifactCorruptError(f"zstd decode failed: {e}") from e
    except tarfile.TarError as e:
        raise ArtifactCorruptError(f"tar extraction failed: {e}") from e

    if not found_library:
        raise ArtifactCorruptError(f"the tarball had no entry named {library_name!r}")
    if library_size != library_bytes:
        raise ArtifactCorruptError(
            f"library size {library_size} disagreed with the signed predicate's {library_bytes}"
        )
    actual_sha256 = library_hash.hexdigest()
    if actual_sha256 != library_sha256:
        raise ArtifactCorruptError(
            f"library sha256 {actual_sha256} disagreed with the signed predicate's {library_sha256}"
        )
