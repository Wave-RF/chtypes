"""The in-use signal (docs/guides/fetch-v1.md §1, "In use"; public issue #494).

A process that is about to load an installed build, or that a registry's
fetch handed one to, takes a SHARED advisory lock (flock) on the entry's
`verified.json` and keeps it until the process exits. `chtypes prune` removes
an entry only after it has taken the EXCLUSIVE lock without waiting, so it
never removes a build a live process holds. The kernel drops a dead process's
locks, so a crash never pins a build. flock belongs to the open file
description on Linux and darwin, so a hold taken by this very process counts
too, and so does one taken by any binding: all four lock the same file the
same way.
"""

from __future__ import annotations

import errno
import os
import threading
from collections.abc import Callable
from pathlib import Path

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._errors import ArtifactMissingError

try:
    import fcntl
except ImportError:  # not a platform chtypes serves: nothing is held, prune keeps everything
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "CLAIM_GONE",
    "CLAIM_IN_USE",
    "CLAIM_OWNED",
    "HELD",
    "UNHELD",
    "VANISHED",
    "claim",
    "hold",
    "removed_while_held",
]

# What `hold` found.
HELD = "held"  # this process holds the entry, shared, for its life
UNHELD = "unheld"  # the record cannot be locked here: go on (prune cannot lock it either)
VANISHED = "vanished"  # the entry is gone or replaced: a prune removed it; look again

# What `claim` found.
CLAIM_OWNED = "owned"  # locked exclusively: no process holds it
CLAIM_IN_USE = "in-use"  # a process holds it, or it cannot be locked: kept
CLAIM_GONE = "gone"  # gone or replaced since it was listed: not ours to report

_GONE = (errno.ENOENT, errno.ENOTDIR)

# This process's shared locks: one open record per entry directory, kept until
# the process exits.
_holds_lock = threading.Lock()
_holds: dict[str, int] = {}


def _same_file(fd: int, path: str) -> bool:
    try:
        held = os.fstat(fd)
        now = os.stat(path)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (now.st_dev, now.st_ino)


def hold(entry_dir: str | os.PathLike[str]) -> str:
    """Take this process's shared hold on the installed entry `entry_dir`
    (`<root>/unpacked/sha256/<manifest hex>`) and keep it for the life of the
    process. It waits only while a prune holds the entry exclusively, which a
    prune does across one rename; then the entry is gone, and this says so."""
    key = str(entry_dir)
    record = os.path.join(key, C.CACHE_VERIFIED_RECORD)
    with _holds_lock:
        fd = _holds.get(key)
        if fd is not None:
            if _same_file(fd, record):
                return HELD
            # The entry this process held was removed and installed again: the
            # old hold protects nothing now.
            os.close(fd)
            del _holds[key]
        try:
            fd = os.open(record, os.O_RDONLY)
        except OSError as exc:
            return VANISHED if exc.errno in _GONE else UNHELD
        if fcntl is None:
            os.close(fd)
            return UNHELD
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)  # retried on EINTR by Python itself (PEP 475)
        except OSError:
            os.close(fd)
            return UNHELD
        if not _same_file(fd, record):
            os.close(fd)
            return VANISHED
        _holds[key] = fd
        return HELD


def claim(entry_dir: str | os.PathLike[str]) -> tuple[str, Callable[[], None] | None]:
    """Take the exclusive lock a prune needs on `entry_dir`, without waiting.
    It is owned only when no process holds the entry and the record it locked
    is still the one at the path; the returned release drops the lock."""
    record = os.path.join(str(entry_dir), C.CACHE_VERIFIED_RECORD)
    try:
        fd = os.open(record, os.O_RDONLY)
    except OSError as exc:
        return (CLAIM_GONE if exc.errno in _GONE else CLAIM_IN_USE), None
    if fcntl is None:
        os.close(fd)
        return CLAIM_IN_USE, None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return CLAIM_IN_USE, None
    if not _same_file(fd, record):
        os.close(fd)
        return CLAIM_GONE, None
    return CLAIM_OWNED, lambda: os.close(fd)


def removed_while_held(request: str, entry_dir: Path) -> ArtifactMissingError:
    """`CHTYPES_ARTIFACT_MISSING` for a request whose build a concurrent prune
    removed twice, each time between its install and this process's hold."""
    return ArtifactMissingError(
        f"chtypes: {entry_dir} was removed by a concurrent prune before this process "
        f"could hold it; ask for {request} again"
    )
