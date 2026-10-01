"""One artifact image, one `chs_init` — keyed on the FILE, not the path (#355).

`dlopen` maps one image per file. A hardlink is a different path to the same
file, so it is handed the image already mapped; a guard keyed on the resolved
path let it re-run `chs_init` on that live image and move the first opener's
zone. These tests drive `Library` — the class that holds the guard — over a
real artifact, copied first so each test owns a FRESH image no other test has
initialized, and read every key back from the code's own stat.

None of them closes a library: the last close runs `chs_shutdown`, and these
images are left to `atexit` like every other image in the session.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import chtypes
from chtypes import Format, Outcome


def _stage(source: chtypes.Library, directory: Path) -> Path:
    """Copy ``source``'s library file, and its refuse-list, into ``directory``
    as a fresh file with its own inode."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / Path(source.path).name
    shutil.copyfile(source.path, target)
    unsafe = Path(source.path).parent / "unsafe_families.txt"
    if unsafe.is_file():
        shutil.copyfile(unsafe, directory / unsafe.name)
    return target


def _epoch(lib: chtypes.Library) -> str:
    """What the live image says about a zone-sensitive value: a bare DateTime
    column given the epoch, rendered under whatever zone `chs_init` last set."""
    with lib.compile_ddl("x DateTime") as schema:
        got = schema.rows(Format.JSON_EACH_ROW, b'{"x": 0}\n')
    assert got.outcome is Outcome.ACCEPTED, got
    return got.rows[0].values[0].text


def test_a_hard_linked_artifact_is_the_same_image(tmp_path: Path, newest: chtypes.Library) -> None:
    """A hardlink under another zone is refused as `InitConflictError` and
    leaves the first opener's answers alone; under the SAME zone it is allowed
    and shares the first opener's image key and lock; the same path twice
    under the same zone stays allowed."""
    orig = _stage(newest, tmp_path / "orig")
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    link = link_dir / orig.name
    os.link(orig, link)

    first = chtypes.Library(str(orig), newest.manifest, "UTC")
    before = _epoch(first)

    with pytest.raises(chtypes.InitConflictError) as refused:
        chtypes.Library(str(link), newest.manifest, "Asia/Tokyo")
    err = refused.value
    assert isinstance(err, chtypes.RegistryError), "the earlier contract's catch must still hold"
    assert (err.path, err.have, err.want) == (str(link), "UTC", "Asia/Tokyo")
    for part in (str(link), "'UTC'", "'Asia/Tokyo'"):
        assert part in str(err), f"{err} does not name {part}"
    assert _epoch(first) == before, "the refused open moved the live image's zone"

    again = chtypes.Library(str(orig), newest.manifest, "UTC")
    via_link = chtypes.Library(str(link), newest.manifest, "UTC")
    # The keys come from the code's own stat; one image, one key, one lock.
    assert again._image_key == first._image_key
    assert via_link._image_key == first._image_key
    assert via_link._native._lock is first._native._lock
    assert _epoch(via_link) == before


def test_a_replaced_file_at_an_open_path_is_the_open_image(
    tmp_path: Path, newest: chtypes.Library
) -> None:
    """The loader matches an open path before it looks at the file, so a NEW
    file (a fresh inode) renamed over an open path is still the open image —
    keyed on the inode alone, it would look new and `chs_init` would re-run
    on the live image."""
    path = _stage(newest, tmp_path / "a")
    first = chtypes.Library(str(path), newest.manifest, "UTC")
    before = _epoch(first)

    old = os.stat(path)
    replacement = path.with_name(path.name + ".new")
    shutil.copyfile(newest.path, replacement)
    replacement.replace(path)
    assert not os.path.samestat(old, os.stat(path)), "precondition: a different file at the path"

    with pytest.raises(chtypes.InitConflictError):
        chtypes.Library(str(path), newest.manifest, "Asia/Tokyo")
    assert _epoch(first) == before, "the refused open moved the live image's zone"
    same = chtypes.Library(str(path), newest.manifest, "UTC")
    assert same._image_key == first._image_key


def test_an_unstattable_artifact_path_is_refused(tmp_path: Path) -> None:
    """The key comes from a stat that follows symlinks; a path it cannot stat
    is refused with the path named, never keyed on its spelling instead. A
    dangling symlink is the case that tells stat from lstat. Needs no artifact:
    the refusal comes before anything is dlopen'd."""
    path = tmp_path / "libchtypes.so"
    path.symlink_to(tmp_path / "missing.so")
    with pytest.raises(chtypes.RegistryError) as refused:
        chtypes.Library(str(path), chtypes.Manifest(library=path.name), "UTC")
    assert not isinstance(refused.value, chtypes.InitConflictError)
    assert isinstance(refused.value.__cause__, FileNotFoundError)
    assert "cannot identify" in str(refused.value)
    assert str(path) in str(refused.value)
