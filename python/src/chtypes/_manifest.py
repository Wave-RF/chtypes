"""The artifact's own description of itself, and the paths every SDK agrees on.

`manifest.json` (spec/artifact.md) is the loader's single source of truth for
the shared library's file name and the only integrity check that means
anything. Shared by the loader (`registry.py`) and the fetcher (`fetch.py`),
which is why it lives apart from both.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
from dataclasses import dataclass, fields
from pathlib import Path

from .errors import RegistryError

__all__ = [
    "Manifest",
    "cache_registry_dir",
    "host_platform",
    "minor_of",
    "read_manifest",
    "verify_library",
]

_ARCH = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def host_platform() -> str:
    """This host's platform key, spelled the artifact way: ``darwin-arm64``,
    ``linux-amd64`` (``<os>`` lowercased, ``aarch64 -> arm64``, ``x86_64 -> amd64``)."""
    machine = _platform.machine()
    return f"{_platform.system().lower()}-{_ARCH.get(machine, machine)}"


def cache_registry_dir(platform: str | None = None) -> str:
    """The per-user artifact cache for a platform — ``${XDG_CACHE_HOME:-~/.cache}/
    chtypes/artifacts/<os>-<arch>``, this host's by default. Where fetch installs,
    where a core-repository build lands, and slot 3 of the registry search path
    (docs/fetch.md §1). A path, not a promise: it need not exist yet."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "chtypes", "artifacts", platform or host_platform())


@dataclass(frozen=True, slots=True)
class Manifest:
    """`manifest.json`, of which only `library` is load-bearing for the loader.

    Unknown fields are ignored rather than rejected, and no field is required
    beyond `library`: the file is allowed to grow.
    """

    library: str
    clickhouse_version: str = ""
    clickhouse_minor: str = ""
    clickhouse_commit: str = ""
    os: str = ""
    arch: str = ""
    library_bytes: int = 0
    library_sha256: str = ""
    unsafe_families: str = ""


def read_manifest(version_dir: str | os.PathLike[str]) -> Manifest | None:
    """Read one version directory's manifest, or None if there is not a usable one.

    A registry may legitimately contain scratch directories, and a `.DS_Store` is
    not a version: an unreadable or unparseable manifest means "skip this
    directory", never an error.
    """
    path = Path(version_dir) / "manifest.json"
    try:
        doc = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("library"), str):
        return None
    known = {f.name for f in fields(Manifest)}
    try:
        return Manifest(**{k: v for k, v in doc.items() if k in known})
    except TypeError:  # pragma: no cover - a field of the wrong type
        return None


def verify_library(version_dir: str | os.PathLike[str]) -> None:
    """Re-hash the shared library and compare it against the manifest.

    The artifact carries its own checksum, so this is neither optional nor
    expensive for anything that arrived over a network: a move that reported
    success and truncated a 232 MB library looks identical to one that worked.
    """
    directory = Path(version_dir)
    manifest = read_manifest(directory)
    if manifest is None:
        raise RegistryError(f"chtypes: no usable manifest.json in {directory}")
    path = directory / manifest.library
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RegistryError(f"chtypes: {path}: {exc}") from exc
    if manifest.library_bytes and size != manifest.library_bytes:
        raise RegistryError(
            f"chtypes: {path} is {size} bytes, manifest says {manifest.library_bytes}"
        )
    if not manifest.library_sha256:
        return
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != manifest.library_sha256:
        raise RegistryError(
            f"chtypes: {path} sha256 {digest.hexdigest()} != manifest {manifest.library_sha256}"
        )


def minor_of(version: str) -> str:
    """The first two dot-separated components: "25.8.28.1-lts" -> "25.8"."""
    parts = version.split(".", 2)
    if len(parts) < 2:
        return version
    return f"{parts[0]}.{parts[1]}"
