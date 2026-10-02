"""The cache: an OCI image layout plus `unpacked/sha256/<manifest-hex>/`.

layout-v2 spec §7.6 / PLAN §3.4 "The resolution rule for --offline and
resolve_installed": the source of truth for what is installed is the
immutable `unpacked/sha256/*/verified.json` record, never `index.json` —
losing an `index.json` race costs only `oras` interop, never correctness.
`index.json` is written by atomic rename, then re-read and re-applied, never
in place (PLAN §3.2 "index-race-reapply").
"""

from __future__ import annotations

import json
import os
import re
import string
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from chtypes._ocifetch import _constants as C

__all__ = [
    "VerifiedRecord",
    "cache_root",
    "list_verified_records",
    "read_verified_record",
    "resolve_cache_root",
    "search_roots",
    "unpacked_dir_for",
    "write_verified_install",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _expand_template(template: str) -> str:
    """``${XDG_CACHE_HOME:-$HOME/.cache}/chtypes/v1``: a small, deliberately
    narrow expansion (bash ``:-`` default plus plain ``$VAR``) rather than a
    shell call, so this never depends on a shell being present."""
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    mapping = {"HOME": os.path.expanduser("~")}
    out = template.replace("${XDG_CACHE_HOME:-$HOME/.cache}", xdg)
    return string.Template(out).safe_substitute(mapping)


def resolve_cache_root(cache_dir: str | os.PathLike[str] | None = None) -> Path:
    """``CHTYPES_CACHE`` (or an explicit override) names the layout directory
    ITSELF, not a parent `chtypes/` to append to (constants: ``cache.root_template``)."""
    if cache_dir is not None:
        return Path(cache_dir)
    env = os.environ.get(C.ENV_CACHE_NAME)
    if env:
        return Path(env)
    return Path(_expand_template(C.CACHE_ROOT_TEMPLATE))


def cache_root(cache_dir: str | os.PathLike[str] | None = None) -> Path:
    return resolve_cache_root(cache_dir)


def search_roots(cache_dir: str | os.PathLike[str] | None = None) -> tuple[Path, ...]:
    """The cache, then the read-only system directories, in that order
    (layout-v2 spec §0 "cache": "Read-only system directories are searched
    after it")."""
    return (resolve_cache_root(cache_dir), *(Path(d) for d in C.SYSTEM_CACHE_DIRS))


def unpacked_dir_for(root: Path, manifest_digest_hex: str) -> Path:
    if not _HEX64.match(manifest_digest_hex):
        raise ValueError(f"not a 64-hex manifest digest: {manifest_digest_hex!r}")
    return root / C.CACHE_UNPACKED_DIR / manifest_digest_hex


@dataclass(frozen=True)
class VerifiedRecord:
    """The contents of one `unpacked/sha256/<hex>/verified.json`."""

    schema: int
    manifest: str
    layer: str
    bundle: str
    platform: str
    version: str
    build: str
    channel: str | None
    predicate: dict
    signed_by: str
    library: str
    index: str | None = None
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "schema": self.schema,
            "manifest": self.manifest,
            "layer": self.layer,
            "bundle": self.bundle,
            "index": self.index,
            "platform": self.platform,
            "version": self.version,
            "build": self.build,
            "channel": self.channel,
            "predicate": self.predicate,
            "signed_by": self.signed_by,
            "library": self.library,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_json(cls, doc: dict) -> VerifiedRecord:
        return cls(
            schema=doc["schema"],
            manifest=doc["manifest"],
            layer=doc["layer"],
            bundle=doc["bundle"],
            index=doc.get("index"),
            platform=doc["platform"],
            version=doc["version"],
            build=doc["build"],
            channel=doc.get("channel"),
            predicate=doc["predicate"],
            signed_by=doc["signed_by"],
            library=doc["library"],
            warnings=tuple(doc.get("warnings", ())),
        )


def _atomic_write(
    path: Path, data: bytes, *, before_rename: Callable[[], None] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if before_rename is not None:
            before_rename()
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_verified_install(
    root: Path,
    manifest_digest: str,
    record: VerifiedRecord,
    *,
    unpacked_tmp_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Install an already-unpacked library directory plus its
    `verified.json`, atomically: the caller unpacks into a sibling temp
    directory first (`_unpack.unpack_tar_zst` does), then this renames that
    whole directory into place and writes the record — temp-then-rename for
    both, never a partial directory visible under the final name."""
    hex_digest = manifest_digest.split(":", 1)[1]
    dest = unpacked_dir_for(root, hex_digest)
    if unpacked_tmp_dir is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # Another process (or an earlier run) already installed the same
            # content-addressed manifest; the bytes are identical by
            # construction, so there is nothing to reconcile.
            pass
        else:
            tmp = str(unpacked_tmp_dir)
            final_tmp = str(dest) + ".installing"
            os.replace(tmp, final_tmp)
            os.replace(final_tmp, dest)
    record_path = dest / C.CACHE_VERIFIED_RECORD
    _atomic_write(record_path, json.dumps(record.to_json(), sort_keys=True).encode("utf-8"))
    return dest


def read_verified_record(dest_dir: Path) -> VerifiedRecord | None:
    record_path = dest_dir / C.CACHE_VERIFIED_RECORD
    try:
        with open(record_path, encoding="utf-8") as f:
            return VerifiedRecord.from_json(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def list_verified_records(
    roots: Sequence[Path],
) -> list[tuple[Path, VerifiedRecord]]:
    """Every `verified.json` under `unpacked/sha256/*/` across every search
    root, each paired with the directory it lives in (`Resolved.dir`)."""
    out: list[tuple[Path, VerifiedRecord]] = []
    for root in roots:
        unpacked_root = root / C.CACHE_UNPACKED_DIR
        if not unpacked_root.is_dir():
            continue
        for entry in sorted(unpacked_root.iterdir()):
            if not entry.is_dir() or not _HEX64.match(entry.name):
                continue
            record = read_verified_record(entry)
            if record is not None:
                out.append((entry, record))
    return out


def _read_index_or_default(index_path: Path) -> dict:
    try:
        with open(index_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"schemaVersion": 2, "manifests": []}


def update_index_json(
    root: Path,
    mutate: Callable[[dict], dict],
    *,
    before_rename: Callable[[], None] | None = None,
    max_attempts: int = 10,
) -> None:
    """Read-merge-write, re-checked immediately before the rename, re-applied
    on top of whatever is there if it moved (PLAN §3.2 "index-race-reapply").

    `index.json` is written by atomic rename, never in place — but a bare
    "read, merge, rename" still loses a concurrent writer's update if we
    rename over it blindly: `before_rename` is the exact gap PLAN §3.2 names
    ("between temp-write and rename"), so the live file is read ONE more
    time right there, immediately before our own `os.replace`. If it no
    longer matches what we started from, our temp file is stale (computed
    from a `current` that is no longer current) — we discard it, re-merge
    on top of the fresh read, and retry, so a competing writer's entry is
    folded in rather than clobbered. `before_rename` itself is test-only:
    the conformance runner uses it to inject a competing writer
    deterministically; it is never set outside tests.
    """
    index_path = root / "index.json"
    base = _read_index_or_default(index_path)
    for attempt in range(max_attempts):
        updated = mutate(base)
        data = json.dumps(updated, sort_keys=True).encode("utf-8")
        index_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(index_path.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if before_rename is not None and attempt == 0:
                before_rename()
            live_now = _read_index_or_default(index_path)
            if live_now != base:
                # Someone else wrote in the gap: our temp was computed from a
                # base that is no longer current. Re-merge on top of the
                # fresh read instead of clobbering it.
                base = live_now
                continue
            os.replace(tmp_name, index_path)
            return
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    raise RuntimeError(
        f"chtypes: cache index.json at {index_path} could not converge after "
        f"{max_attempts} attempts (a persistent concurrent writer)"
    )
