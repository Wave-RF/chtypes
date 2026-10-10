"""`chtypes prune` (docs/guides/fetch-v1.md §1, "Pruning"; public issue #494).

Remove the installed builds of a line that newer installed builds of the same
line and platform supersede, keeping the newest N, and never one a live process
holds (`_hold`). It reads and writes the cache only, never a system directory,
and never another fingerprint's records (§3: the dev channel's own-fingerprint
rule). Go, TypeScript and Rust give the same answer on the same cache.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chtypes._ocifetch import _channel
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._ensure import Options
from chtypes._ocifetch._faults import probe_roots, unwritable
from chtypes._ocifetch._hold import CLAIM_GONE, CLAIM_IN_USE, claim
from chtypes._ocifetch._layout import (
    VerifiedRecord,
    list_verified_records,
    search_roots,
    update_index_json,
)
from chtypes._ocifetch._oci import spelling_components

__all__ = ["Superseded", "is_line", "line_of", "prune"]

_LINE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def is_line(spelling: str) -> bool:
    """Whether `spelling` is a two-part line, such as `26.8`."""
    return _LINE.fullmatch(spelling) is not None


def line_of(version: str) -> str:
    """The two-part line of a four-part version: `26.8` for `26.8.15.10`."""
    parts = version.split(".")
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else ""


@dataclass(frozen=True)
class Superseded:
    """One installed build a prune found superseded: removed (in a dry run, to
    be removed), or kept because it is `in_use` (a live process holds it, or
    its record cannot be locked at all, so nothing can tell)."""

    platform: str
    version: str
    build: str
    manifest: str  # the entry's own name: sha256:<unpacked/sha256/<hex>>
    dir: Path
    in_use: bool


def prune(
    options: Options, *, line: str | None = None, keep: int = 1, dry_run: bool = False
) -> list[Superseded]:
    """Remove, from the cache, every installed build that at least `keep` newer
    installed builds of its own line and platform supersede. Builds are ordered
    by (version, build), and the report by platform (the constants' order),
    then oldest first. A superseded build a live process holds is reported
    `in_use` and kept. For each build it removes the unpacked entry with its
    record, then its index.json entries, then its blobs (the manifest, its
    config and layer, and every referrer manifest of it the layout holds, with
    that referrer's layers), never a blob a remaining build names."""
    if keep < 1:
        raise ValueError(
            "chtypes: --keep is at least 1 (the newest build of a line is never superseded), "
            f"not {keep}"
        )
    if line is not None and not is_line(line):
        raise ValueError(f"chtypes: --line takes a two-part line such as 26.8, not {line!r}")
    _channel.enforce(options)
    cache = search_roots(options.cache_dir, options.system_dirs)[0]
    try:
        return _prune(cache, options, line, keep, dry_run)
    except OSError as exc:
        typed = unwritable(exc, [cache])
        if typed is None:
            raise
        raise typed from exc


def _age(entry: tuple[Path, VerifiedRecord]) -> tuple:
    entry_dir, record = entry
    return (spelling_components(record.version), record.build, entry_dir.name)


def _platform_rank(key: str) -> int:
    for i, p in enumerate(C.PLATFORMS):
        if p["key"] == key:
            return i
    return len(C.PLATFORMS)


def _prune(
    cache: Path, options: Options, line: str | None, keep: int, dry_run: bool
) -> list[Superseded]:
    probe_roots([cache], options.resolved_strict())
    groups: dict[tuple[str, str], list[tuple[Path, VerifiedRecord]]] = {}
    for entry_dir, record in list_verified_records([cache]):
        if not _channel.visible(record.predicate):
            continue  # another fingerprint's build: its own SDK keeps or prunes it
        record_line = line_of(record.version)
        if line is not None and record_line != line:
            continue
        groups.setdefault((record.platform, record_line), []).append((entry_dir, record))
    superseded: list[tuple[Path, VerifiedRecord]] = []
    for members in groups.values():
        members.sort(key=_age, reverse=True)
        superseded.extend(members[keep:])
    superseded.sort(key=lambda e: (_platform_rank(e[1].platform), _age(e)))

    unpacked_root = cache / C.CACHE_UNPACKED_DIR
    out: list[Superseded] = []
    removed: list[tuple[VerifiedRecord, str, Path]] = []
    for entry_dir, record in superseded:
        manifest = f"sha256:{entry_dir.name}"
        state, release = claim(entry_dir)
        if state == CLAIM_GONE:
            continue  # another prune removed it, or an install replaced it
        report = Superseded(
            platform=record.platform,
            version=record.version,
            build=record.build,
            manifest=manifest,
            dir=entry_dir,
            in_use=state == CLAIM_IN_USE,
        )
        if release is None:
            out.append(report)
            continue
        if dry_run:
            release()
            out.append(report)
            continue
        # Moved aside while the exclusive lock is held: once it drops, no
        # lookup lists the entry, and a hold taken on it finds it gone.
        try:
            aside = Path(tempfile.mkdtemp(dir=str(unpacked_root), prefix=".prune-"))
            try:
                os.rename(entry_dir, aside / "entry")
            except OSError:
                shutil.rmtree(aside, ignore_errors=True)
                raise
        finally:
            release()
        removed.append((record, manifest, aside))
        out.append(report)
    if not removed:
        return out

    blobs = _blobs_of(cache, {manifest for _, manifest, _ in removed})
    for record, _, _ in removed:
        for digest in (record.layer, record.bundle, record.bundle_manifest):
            if digest and _DIGEST.match(digest):
                blobs.add(digest)
    # Never a blob a remaining build names, whatever its fingerprint.
    for entry_dir, record in list_verified_records([cache]):
        named = (record.manifest, record.layer, record.bundle, record.bundle_manifest)
        for digest in (*_own_blobs(cache, f"sha256:{entry_dir.name}"), *named):
            blobs.discard(digest)
    _remove_index_entries(cache, blobs)
    for digest in blobs:
        try:
            os.unlink(_blob_path(cache, digest))
        except FileNotFoundError:
            pass
    for _, _, aside in removed:
        shutil.rmtree(aside)
    return out


def _blob_path(cache: Path, digest: str) -> Path:
    return cache / "blobs" / "sha256" / digest.split(":", 1)[1]


def _own_blobs(cache: Path, manifest: str) -> list[str]:
    """The blobs a manifest names itself: the manifest, its config and its
    layers (only the manifest when its blob is absent or unreadable)."""
    out = [manifest]
    try:
        doc = json.loads(_blob_path(cache, manifest).read_bytes())
    except (OSError, ValueError):
        return out
    if not isinstance(doc, dict):
        return out
    config = doc.get("config")
    if (
        isinstance(config, dict)
        and isinstance(config.get("digest"), str)
        and _DIGEST.match(config["digest"])
    ):
        out.append(config["digest"])
    out.extend(_layer_digests(doc))
    return out


def _layer_digests(doc: dict) -> list[str]:
    layers = doc.get("layers")
    if not isinstance(layers, list):
        return []
    return [
        layer["digest"]
        for layer in layers
        if isinstance(layer, dict)
        and isinstance(layer.get("digest"), str)
        and _DIGEST.match(layer["digest"])
    ]


def _blobs_of(cache: Path, manifests: set[str]) -> set[str]:
    """The blobs a prune removes with `manifests`: each manifest's own, and
    every referrer manifest the layout holds whose subject is one of them (a
    signature, a goldens document), with that referrer's layers. A referrer's
    config, the empty `{}` every referrer shares, is never among them."""
    out: set[str] = set()
    for manifest in manifests:
        out.update(_own_blobs(cache, manifest))
    blob_dir = cache / "blobs" / "sha256"
    try:
        names = sorted(os.listdir(blob_dir))
    except OSError:
        return out
    for name in names:
        if not _HEX64.match(name):
            continue
        path = blob_dir / name
        try:
            if not path.is_file() or path.stat().st_size > C.MANIFEST_MAX_BYTES:
                continue
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        subject = doc.get("subject")
        if not isinstance(subject, dict) or subject.get("digest") not in manifests:
            continue
        out.add(f"sha256:{name}")
        out.update(_layer_digests(doc))
    return out


def _remove_index_entries(cache: Path, drop: set[str]) -> None:
    """Drop every index.json entry whose digest is in `drop`, by the same
    read-check-rename loop an install's entry uses, so an entry another process
    adds meanwhile survives. An absent or unparsable index.json is left as it
    is."""
    try:
        with open(cache / "index.json", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return
    except ValueError:
        return
    if not isinstance(doc, dict) or not isinstance(doc.get("manifests"), list):
        return

    def dropped(entry: object) -> bool:
        return isinstance(entry, dict) and entry.get("digest") in drop

    if not any(dropped(e) for e in doc["manifests"]):
        return

    def _drop(current: dict) -> dict:
        return {
            **current,
            "manifests": [e for e in (current.get("manifests") or []) if not dropped(e)],
        }

    update_index_json(cache, _drop)
