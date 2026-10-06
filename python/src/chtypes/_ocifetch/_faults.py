"""What can be wrong with a cache root, and what each mode does about it
(docs/guides/fetch-v1.md §1, the cache faults; public issue #486).

The default mode keeps "unreadable is absent" and says so in a warning; strict
mode (`strict_cache`, `CHTYPES_CACHE_STRICT=1`, `--strict`) makes every fault a
`CacheUnusableError` naming the path, and never falls through to a system dir.
A write the fetch layer needed that failed is `CacheUnusableError` in every
mode. Go, TypeScript and Rust probe the same way, in the same order, and say the
same sentences.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._errors import CacheUnusableError
from chtypes._ocifetch._layout import VerifiedRecord, _no_duplicates, zero_x_hint, zero_x_shape

__all__ = [
    "REASON_LAYOUT_0X",
    "REASON_NOT_A_DIRECTORY",
    "REASON_UNACCEPTABLE_RECORD",
    "REASON_UNREADABLE_ENTRY",
    "REASON_UNREADABLE_ROOT",
    "REASON_UNWRITABLE",
    "cache_error",
    "errno_name",
    "probe_roots",
    "unwritable",
]

REASON_UNREADABLE_ROOT = "unreadable_root"  # K1: the root or unpacked/sha256/ cannot be listed
REASON_NOT_A_DIRECTORY = "not_a_directory"  # K1: the root or unpacked/sha256/ is not a directory
REASON_UNREADABLE_ENTRY = "unreadable_entry"  # K2: an entry or its verified.json cannot be read
REASON_UNACCEPTABLE_RECORD = "unacceptable_record"  # K3, strict only: nothing to re-verify from
REASON_LAYOUT_0X = "layout_0x"  # K4, strict only: a 0.x registry directory
REASON_UNWRITABLE = "unwritable"  # any mode: a write the fetch layer needed failed

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def errno_name(exc: BaseException) -> str:
    """The errno spelling of an OSError ("EACCES"), or "" when it has none."""
    code = getattr(exc, "errno", None)
    if not isinstance(code, int):
        return ""
    return errno.errorcode.get(code, f"errno {code}")


def cache_error(
    path: str | os.PathLike[str], reason: str, os_error: str = ""
) -> CacheUnusableError:
    """The `CHTYPES_CACHE_UNUSABLE` for one fault: the same sentence in every
    binding, "<path> is unusable as a cache: <reason> (<errno>)"."""
    detail = reason + (f" ({os_error})" if os_error else "")
    if reason == REASON_LAYOUT_0X:
        hint = zero_x_hint(Path(path))
        if hint is not None:
            detail += f". {hint}"
    return CacheUnusableError(
        f"chtypes: {path} is unusable as a cache: {detail}",
        path=str(path),
        reason=reason,
        os_error=os_error or None,
    )


@dataclass(frozen=True)
class _Fault:
    path: str
    reason: str
    os_error: str = ""

    @property
    def warns(self) -> bool:
        return self.reason in (
            REASON_UNREADABLE_ROOT,
            REASON_NOT_A_DIRECTORY,
            REASON_UNREADABLE_ENTRY,
        )

    def warning(self) -> str:
        return (
            f"{self.path} could not be read ({self.os_error}); treated as not installed. "
            f"Set {C.ENV_CACHE_STRICT_NAME}=1 to make this an error."
        )


def _denied(path: Path) -> bool:
    try:
        os.stat(path)
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def _probe_root(root: Path, is_cache: bool) -> list[_Fault]:
    """What is wrong with one root, in a fixed order: the root itself, then the
    0.x shape (the cache only), then each entry by name. A root that does not
    exist is the empty cache, not a fault."""
    try:
        st = os.stat(root)
    except FileNotFoundError:
        return []
    except NotADirectoryError as e:
        return [_Fault(str(root), REASON_NOT_A_DIRECTORY, errno_name(e))]
    except OSError as e:
        return [_Fault(str(root), REASON_UNREADABLE_ROOT, errno_name(e))]
    if not stat.S_ISDIR(st.st_mode):
        return [_Fault(str(root), REASON_NOT_A_DIRECTORY, "ENOTDIR")]
    if is_cache and zero_x_shape(root) is not None:
        return [_Fault(str(root), REASON_LAYOUT_0X)]
    unpacked = root / "unpacked"
    sha = root / C.CACHE_UNPACKED_DIR
    try:
        with os.scandir(sha) as it:
            entries = sorted((e for e in it), key=lambda e: e.name)
    except FileNotFoundError:
        return []
    except NotADirectoryError:
        path = unpacked if unpacked.exists() and not unpacked.is_dir() else sha
        return [_Fault(str(path), REASON_NOT_A_DIRECTORY, "ENOTDIR")]
    except OSError as e:
        # The path that blocks the listing: the first one that cannot be
        # passed through, else unpacked/sha256 itself.
        path = root if _denied(unpacked) else unpacked if _denied(sha) else sha
        return [_Fault(str(path), REASON_UNREADABLE_ROOT, errno_name(e))]
    faults: list[_Fault] = []
    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if not is_dir or not _HEX64.match(entry.name):
            continue
        record = Path(entry.path) / C.CACHE_VERIFIED_RECORD
        try:
            with open(record, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            continue  # no record yet: a pre-seed or an unfinished install
        except OSError as e:
            path = entry.path if _denied(record) else str(record)
            faults.append(_Fault(path, REASON_UNREADABLE_ENTRY, errno_name(e)))
            continue
        if (
            is_cache
            and not _acceptable(raw)
            and not (root / "blobs" / "sha256" / entry.name).exists()
        ):
            faults.append(_Fault(str(record), REASON_UNACCEPTABLE_RECORD))
    return faults


def _acceptable(raw: bytes) -> bool:
    try:
        VerifiedRecord.from_json(json.loads(raw, object_pairs_hook=_no_duplicates))
    except (ValueError, RecursionError):
        return False
    return True


def probe_roots(roots: Sequence[Path], strict: bool) -> list[str]:
    """Check the cache (`roots[0]`), then every system dir. In strict mode the
    first fault is raised as its `CacheUnusableError`, the cache's before any
    system dir's, so nothing falls through. In the default mode, one warning
    per unusable root or unreadable entry is returned. A system dir that does
    not exist is skipped in both, as a default list."""
    warnings: list[str] = []
    for i, root in enumerate(roots):
        for fault in _probe_root(Path(root), is_cache=i == 0):
            if strict:
                if i > 0 and not fault.warns:
                    continue
                raise cache_error(fault.path, fault.reason, fault.os_error)
            if fault.warns:
                warnings.append(fault.warning())
    return warnings


def unwritable(exc: OSError, roots: Sequence[Path]) -> CacheUnusableError | None:
    """A filesystem failure of a write the fetch layer needed, under the cache,
    as `CacheUnusableError` with reason `unwritable` in every mode (never a raw
    `OSError`). `None` for an error that names no path under the cache (the
    lock file, a `file://` base, or a socket's)."""
    cache = os.path.abspath(roots[0])
    for name in (getattr(exc, "filename2", None), getattr(exc, "filename", None)):
        if name is None:
            continue
        path = os.path.abspath(os.fsdecode(name))
        if path == cache or path.startswith(cache + os.sep):
            return cache_error(path, REASON_UNWRITABLE, errno_name(exc))
    return None
